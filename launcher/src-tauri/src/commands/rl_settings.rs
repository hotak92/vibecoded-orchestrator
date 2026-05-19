//! Stream 2 (2026-05-19): per-project RL Reranker settings + global
//! retraining commands.
//!
//! These commands back the controls declared in
//! `paid-modules/vct-rl-reranker/vct-module.json`'s `gui.config_tab`
//! block. Settings are stored in `module_settings` (the existing
//! per-project, per-module KV blob table) under `module_id =
//! "vct-rl-reranker"` so the same persistence path that backs ANY
//! schema-rendered tab's generic state also drives the RL-specific
//! flags.
//!
//! Three flags are persisted per-project (boolean, default false):
//!
//!   * `rl_use_global` — "read-only global mode". When true, online
//!     training events from this project DO NOT update the local model.
//!   * `rl_online_training_disabled` — freezes the local model AND
//!     marks new events as log-only. Independent of `rl_use_global`.
//!   * `rl_global_training_source_flag` — opts this project's data
//!     INTO the global model's retraining corpus. Independent of the
//!     two flags above.
//!
//! Reset / retrain commands STUB for Stream 2: the RL container isn't
//! built yet (Stream 3). They record the intent in `module_settings`
//! (so the GUI can show "last reset: 2026-05-19" after Stream 3 wires
//! the real call) and return a `RetrainResult { ok: true, message:
//! "stubbed" }`. Each stub carries a `// TODO(Stream 3)` marker.
//!
//! Side-effect contract: the `on_change` Tauri commands declared in
//! the manifest must accept the standard `{ moduleId, value, projectId
//! }` envelope the schema renderer sends. We add a `project_id` arg to
//! every command because per-project flags are inherently per-project;
//! the schema renderer will be extended (Part C) to forward the active
//! project id from the Sidebar's selected-project store.

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

#[derive(Debug, Clone, Serialize)]
pub struct RetrainResult {
    pub ok: bool,
    pub message: String,
    /// Echo of the project ids that would be used as training source.
    /// Useful for the toast confirmation message in the GUI.
    pub project_ids: Vec<String>,
}

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
/// TODO(Stream 3): POST to `rl_server` container at
/// `/projects/<project_id>/reset?strategy=fork`. Confirm container
/// reachable; reflect failure in the toast.
#[command]
pub async fn rl_reset_to_global(
    project_id: String,
    db: State<'_, Db>,
) -> Result<RetrainResult, String> {
    if project_id.is_empty() {
        return Err("rl_reset_to_global: project_id required".into());
    }
    // Persist an audit trail so the GUI can show "last reset: ..."
    // after Stream 3 lands. Soft-fail: write failure doesn't break
    // the command since the real reset hasn't happened yet anyway.
    let _ = db.set_setting(
        &project_id,
        MODULE_ID,
        "rl_last_reset_intent",
        &serde_json::json!({
            "kind": "reset_to_global",
            "at": chrono::Utc::now().timestamp_millis(),
        }),
    );
    Ok(RetrainResult {
        ok: true,
        message: "not yet wired to container (Stream 3 will connect to rl_server /reset)".into(),
        project_ids: vec![project_id],
    })
}

/// "Reset + offline-specialize" — reset to global, then run an offline
/// training pass on this project's recent events. STUB for Stream 2.
///
/// TODO(Stream 3): orchestrate two-stage call to the RL container —
/// `/projects/<id>/reset?strategy=fork` followed by
/// `/projects/<id>/specialize?days=30`.
#[command]
pub async fn rl_reset_and_specialize(
    project_id: String,
    db: State<'_, Db>,
) -> Result<RetrainResult, String> {
    if project_id.is_empty() {
        return Err("rl_reset_and_specialize: project_id required".into());
    }
    let _ = db.set_setting(
        &project_id,
        MODULE_ID,
        "rl_last_reset_intent",
        &serde_json::json!({
            "kind": "reset_and_specialize",
            "at": chrono::Utc::now().timestamp_millis(),
        }),
    );
    Ok(RetrainResult {
        ok: true,
        message: "not yet wired to container (Stream 3 will run reset + offline specialize)".into(),
        project_ids: vec![project_id],
    })
}

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

// ─── Global retrain (STUBS for Stream 2) ────────────────────────────────

/// "Retrain global (online, from current state)" — replays recent
/// events from selected projects through the existing global model.
/// STUB for Stream 2.
///
/// TODO(Stream 3): POST to the container's `/global/retrain?mode=online`
/// with the selected project ids. Stream progress events back over a
/// Tauri channel.
#[command]
pub async fn retrain_global_online(
    project_ids: Vec<String>,
    _db: State<'_, Db>,
) -> Result<RetrainResult, String> {
    if project_ids.is_empty() {
        return Err("retrain_global_online: select at least one source project".into());
    }
    Ok(RetrainResult {
        ok: true,
        message: format!(
            "not yet wired to container (Stream 3 will replay events from {} project(s) through the live global model)",
            project_ids.len()
        ),
        project_ids,
    })
}

/// "Retrain global (offline pass, from scratch)" — rebuilds the global
/// model from all selected projects' historical events. STUB for
/// Stream 2.
///
/// TODO(Stream 3): POST to `/global/retrain?mode=offline`. Streams
/// progress; final result lands in the container's weights volume.
#[command]
pub async fn retrain_global_offline(
    project_ids: Vec<String>,
    _db: State<'_, Db>,
) -> Result<RetrainResult, String> {
    if project_ids.is_empty() {
        return Err("retrain_global_offline: select at least one source project".into());
    }
    Ok(RetrainResult {
        ok: true,
        message: format!(
            "not yet wired to container (Stream 3 will rebuild the global model offline from {} project(s)' full history)",
            project_ids.len()
        ),
        project_ids,
    })
}

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
