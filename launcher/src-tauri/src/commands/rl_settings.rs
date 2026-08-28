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
// v0.2.91 WP-L: the ONE home of the dual-flag precedence + cascade. The six
// dual_* commands in this file delegate to it so they cannot disagree with
// the hub `/config` resolver, which calls the same function.
use vct_launcher_core::db::settings::DualFlag;

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

// ─── v0.2.71 T-B-flags → v0.2.91 WP-L: addressing moved to `DualFlag` ────
//
// This file used to carry its own copies of the dual flags' `module_id` +
// `setting_key` strings plus a pair of module-id-parameterised row helpers.
// v0.2.91 WP-L gave those flags a host-wide default tier, and with it ONE
// home for their addressing AND their precedence:
// `vct_launcher_core::db::settings::DualFlag` (+ `Db::resolve_dual_flags` /
// `Db::set_dual_flag_for_project`), which the hub `/config` resolver calls
// too.
//
// The local copies were deleted rather than left in place: a second table of
// the same strings, now read by nobody in production, is exactly the shape
// that drifts silently. `DualFlag::{module_id, setting_key, app_state_key}`
// is the SSOT, pinned by `settings.rs::addressing_table_is_stable` and
// cross-checked against the Python projection by
// `tests/test_dual_flags_cascade_parity_v0291.py`.
//
// `MODULE_ID` above stays — the three ORIGINAL RL flags (`rl_use_global` et
// al.) are unrelated to the dual family and still live under it.

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
    // v0.2.91 WP-L: the coherence cascade that used to live inline here now
    // lives in `Db::set_dual_flag_for_project` — see the delegation note on
    // `get_dual_embedding_write_all_slots` below. Behaviour is unchanged for
    // this entry point (write the row, force the dependent off when the
    // prerequisite goes off), except that the dependent is only written when
    // it would otherwise RESOLVE on, so an already-off project no longer
    // gains a redundant row.
    db.set_dual_flag_for_project(project_id, DualFlag::WriteAllSlots, Some(value))?;
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

/// Read the EFFECTIVE "write embeddings to ALL named-vector slots" flag.
///
/// v0.2.91 WP-L (decision #22) — DELEGATION NOTE, applies to all three
/// `get_dual_*` commands and all three `set_dual_*_with_db` functions below.
///
/// These commands predate the host-wide default tier. Their bare-`bool`
/// signature reads "no per-project row" as `false`, which stopped being
/// correct the moment a project could inherit an install-wide `true`: a
/// caller here would have seen `false` while the hub `/config` resolver and
/// the env projection both served `true`. That is the Defect-D class of
/// GUI-write-vs-hub-read disagreement, so the bodies now delegate to the ONE
/// resolver (`vct_launcher_core::db::settings::Db::resolve_dual_flags`) that
/// the hub also calls, and cannot drift from it.
///
/// The signature is deliberately unchanged — these are a shipped IPC surface.
/// What they cannot express is PROVENANCE (is this value this project's
/// choice or an inherited default?), which is why new GUI code calls
/// `commands::dual_flags::get_dual_flags_state` instead. Rendering a
/// checkbox from the value returned here would be the lying toggle.
#[command]
pub async fn get_dual_embedding_write_all_slots(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_embedding_write_all_slots: project_id required".into());
    }
    Ok(db.resolve_dual_flags(&project_id).write_all_slots.effective)
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
    // v0.2.91 WP-L: delegates to the ONE cascade (see the delegation note on
    // `get_dual_embedding_write_all_slots`). The prerequisite is still
    // force-enabled BEFORE the dependent is written, so no observer can read
    // (log=true, write=false) — but only when dual-write does not already
    // RESOLVE on, so a project inheriting a host-wide `write = true` keeps
    // inheriting instead of being silently pinned to an explicit row.
    db.set_dual_flag_for_project(project_id, DualFlag::RlLog, Some(value))?;
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

/// Read the EFFECTIVE "also log RL events under the secondary embedding
/// slot" flag — after the host-wide default tier AND the log⟹write clamp.
/// See the delegation note on `get_dual_embedding_write_all_slots`.
#[command]
pub async fn get_dual_rl_log_enabled(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_rl_log_enabled: project_id required".into());
    }
    Ok(db.resolve_dual_flags(&project_id).rl_log.effective)
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
    // v0.2.91 WP-L: delegates to the ONE writer (see the delegation note on
    // `get_dual_embedding_write_all_slots`). Still cascade-free in both
    // directions — arctic-secondary is independent of the other two.
    db.set_dual_flag_for_project(project_id, DualFlag::ArcticSecondary, Some(value))?;
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

/// Read the EFFECTIVE "also write embeddings into a secondary arctic slot"
/// flag — after the host-wide default tier. See the delegation note on
/// `get_dual_embedding_write_all_slots`.
#[command]
pub async fn get_dual_embedding_arctic_secondary(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_embedding_arctic_secondary: project_id required".into());
    }
    Ok(db.resolve_dual_flags(&project_id).arctic_secondary.effective)
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

    /// Read a dual flag's RAW per-project row (no cascade, no host-wide
    /// tier) — what most tests below assert on, because they are pinning the
    /// setters' write behaviour rather than the resolver's answer.
    ///
    /// Addressed through `DualFlag` so these fixtures cannot drift from the
    /// production strings; the old local const table was deleted in WP-L.
    /// `Result` shape kept so the existing `.unwrap()` call sites read the
    /// same as before.
    fn dual_row(db: &Db, project_id: &str, flag: DualFlag) -> Result<bool, String> {
        Ok(db.dual_flag_explicit(project_id, flag).unwrap_or(false))
    }

    /// Write a dual flag's raw per-project row, bypassing the coherence
    /// cascade — for seeding states the setters would refuse to produce.
    fn set_dual_row(
        db: &Db,
        project_id: &str,
        flag: DualFlag,
        value: bool,
    ) -> Result<(), String> {
        db.set_setting(
            project_id,
            flag.module_id(),
            flag.setting_key(),
            &serde_json::Value::Bool(value),
        )
    }

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

        assert!(!dual_row(&db, p, DualFlag::WriteAllSlots)
        .unwrap());
        assert!(!dual_row(&db, p, DualFlag::RlLog)
        .unwrap());
    }

    /// Per-project `true` overrides the default. Writing one flag does NOT
    /// flip the other (independent rows under distinct module_ids).
    #[test]
    fn dual_flags_set_true_independently() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        set_dual_row(&db, p, DualFlag::WriteAllSlots, true)
        .unwrap();
        assert!(dual_row(&db, p, DualFlag::WriteAllSlots)
        .unwrap());
        // dual_rl_log untouched.
        assert!(!dual_row(&db, p, DualFlag::RlLog).unwrap());
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
        assert!(!dual_row(&db, p, DualFlag::WriteAllSlots)
        .unwrap());

        apply_set_dual_rl_log(&db, p, true).unwrap();

        assert!(
            dual_row(&db, p, DualFlag::RlLog).unwrap(),
            "dual_rl_log must be true after enabling",
        );
        assert!(
            dual_row(&db, p, DualFlag::WriteAllSlots)
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
        assert!(dual_row(&db, p, DualFlag::RlLog).unwrap());

        // Now turn the prerequisite OFF — the dependent must cascade off.
        apply_set_dual_write(&db, p, false).unwrap();

        assert!(
            !dual_row(&db, p, DualFlag::WriteAllSlots)
            .unwrap(),
            "dual_embedding_write_all_slots must be false after disable",
        );
        assert!(
            !dual_row(&db, p, DualFlag::RlLog).unwrap(),
            "disabling the prerequisite must cascade the dependent off",
        );
    }

    // ─── v0.2.88 (DEFECT 5): arctic-secondary dual-write flag ────────────

    /// Default OFF on a missing row (opt-in), same as the two siblings.
    #[test]
    fn arctic_secondary_defaults_false() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        assert!(!dual_row(&db, p, DualFlag::ArcticSecondary)
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
        assert!(dual_row(&db, p, DualFlag::ArcticSecondary)
        .unwrap());
        assert!(
            !dual_row(&db, p, DualFlag::WriteAllSlots)
            .unwrap(),
            "arctic-secondary must NOT force-enable write_all_slots",
        );
        assert!(
            !dual_row(&db, p, DualFlag::RlLog).unwrap(),
            "arctic-secondary must NOT touch dual_rl_log",
        );

        // Enabling write_all_slots must NOT flip arctic-secondary off.
        set_dual_embedding_write_all_slots_with_db(&db, p, true).unwrap();
        assert!(
            dual_row(&db, p, DualFlag::ArcticSecondary)
            .unwrap(),
            "toggling write_all_slots must leave arctic-secondary untouched",
        );

        // Disabling write_all_slots (which cascades dual_rl_log off) must ALSO
        // leave arctic-secondary untouched (it's not part of that cascade).
        set_dual_embedding_write_all_slots_with_db(&db, p, false).unwrap();
        assert!(
            dual_row(&db, p, DualFlag::ArcticSecondary)
            .unwrap(),
            "the write_all_slots→dual_rl_log cascade must NOT reach arctic-secondary",
        );
    }

    // ─── v0.2.91 WP-L: the delegation is GUARDED ─────────────────────────
    //
    // Every test above reaches the DB through the private row helpers
    // (`get_bool_flag_for_module`), which is why they all still pass after
    // the six commands were re-pointed at `Db::resolve_dual_flags`. That
    // also means they can no longer fail when the RESOLVER is wrong: they
    // measure the row, not the answer the commands now return. The three
    // below close that gap by reading through the delegating path in the
    // exact state where row-reading and resolving diverge.

    /// Read the value the delegating `get_dual_*` command bodies produce.
    /// (The `#[command]` wrappers need Tauri `State`; the body is one line
    /// over `resolve_dual_flags`, so drive that directly.)
    fn effective_via_delegation(db: &Db, project_id: &str) -> (bool, bool, bool) {
        let s = db.resolve_dual_flags(project_id);
        (
            s.write_all_slots.effective,
            s.rl_log.effective,
            s.arctic_secondary.effective,
        )
    }

    /// The state the OLD bodies got wrong: no per-project row, host-wide
    /// default ON. Row-reading says false; the hub says true. A getter that
    /// still read the row would fail here and nowhere else.
    #[test]
    fn getters_report_the_inherited_host_wide_default() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        db.set_dual_flag_global_default(DualFlag::WriteAllSlots, true)
            .unwrap();
        db.set_dual_flag_global_default(DualFlag::ArcticSecondary, true)
            .unwrap();

        // The raw rows are still absent — this is inheritance, not a write.
        assert!(!dual_row(&db, p, DualFlag::WriteAllSlots)
        .unwrap());

        let (write, _log, arctic) = effective_via_delegation(&db, p);
        assert!(
            write,
            "the getter must report the host-wide default the hub also serves",
        );
        assert!(arctic);
    }

    /// An explicit per-project `false` must beat a host-wide `true` through
    /// the delegating getters too (decision #22, both directions).
    #[test]
    fn getters_honour_an_explicit_project_optout() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        db.set_dual_flag_global_default(DualFlag::ArcticSecondary, true)
            .unwrap();
        set_dual_embedding_arctic_secondary_with_db(&db, p, false).unwrap();

        let (_w, _l, arctic) = effective_via_delegation(&db, p);
        assert!(!arctic, "an explicit per-project OFF must survive a host-wide ON");
    }

    /// The cross-tier clamp reaches the legacy getters: a host-wide log
    /// default meeting an explicit per-project write=false must read as
    /// log=false here, not just in the hub.
    #[test]
    fn getters_apply_the_cross_tier_clamp() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        db.set_dual_flag_global_default(DualFlag::RlLog, true).unwrap();
        // Raw row write so the project tier holds write=false under a
        // host-wide log=true (the setter's own cascade would prevent it).
        set_dual_row(&db, p, DualFlag::WriteAllSlots, false)
        .unwrap();

        let (write, log, _a) = effective_via_delegation(&db, p);
        assert!(!write);
        assert!(
            !log,
            "the legacy getter must never report the incoherent \
             (log=true, write=false) pair either",
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
