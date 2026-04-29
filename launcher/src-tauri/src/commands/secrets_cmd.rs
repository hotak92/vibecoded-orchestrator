//! Tauri commands exposing secrets + settings to the React UI.
//!
//! Secret values NEVER leave the Rust process. Commands return presence
//! booleans and masked previews only. Settings are non-sensitive and are
//! returned fully.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::secrets::{self, SecretScope};

// ─── Secrets ────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct SecretMetadata {
    pub key: String,
    pub scope: String,
    pub is_set: bool,
    pub sensitive: bool,
    pub value_preview: Option<String>,
}

fn scope_from_manifest<'a>(scope: &str, project_id: &'a str) -> SecretScope<'a> {
    match scope {
        "global" => SecretScope::Global,
        "shared" => SecretScope::Shared { project_id },
        _ => SecretScope::PerProject { project_id },
    }
}

/// Sentinel project_id used by the GUI when scope is global / shared.
///
/// These scopes don't tie a secret to a specific project; the frontend
/// passes a stable sentinel so the audit log + keychain service name
/// remain well-formed. `_global_` for global scope, `_user_shared_` for
/// shared (per-user, across all projects).
const SENTINEL_GLOBAL: &str = "_global_";
const SENTINEL_SHARED: &str = "_user_shared_";

/// Reject path-traversal-ish project_ids and enforce that per-project
/// secrets target a project that actually exists in the DB.
///
/// Without this, a caller could write a secret under e.g.
/// `project_id = "../../"` (which would still produce a valid keychain
/// service name) or under a `project_id` that no longer corresponds to a
/// registered project. Either lets a per-project secret leak to / be read
/// by an unintended context. Project-isolation requirement (see PR
/// description "Read semantics").
fn enforce_scope_invariants(scope: &str, project_id: &str, db: &Db) -> Result<(), String> {
    // No control characters / dot-segments / slashes anywhere — applies to
    // every scope as a defence-in-depth check. Sentinels above pass.
    if project_id.is_empty()
        || project_id.contains('/')
        || project_id.contains('\\')
        || project_id.contains('\0')
        || project_id == "."
        || project_id == ".."
        || project_id.starts_with("./")
        || project_id.starts_with("../")
    {
        return Err(format!("invalid project_id: {:?}", project_id));
    }
    match scope {
        "global" => {
            if project_id != SENTINEL_GLOBAL {
                return Err(format!(
                    "global scope must use sentinel project_id={:?}; got {:?}",
                    SENTINEL_GLOBAL, project_id
                ));
            }
        }
        "shared" => {
            // Shared keychain still uses a project_id slot for backward
            // compat with existing entries (see secrets.rs `service_name`).
            // The frontend passes `SENTINEL_SHARED` so all "shared"
            // secrets land in one user-wide bucket. Real project ids are
            // also accepted here (legacy) to preserve any pre-existing
            // per-project shared entries written before this PR.
            if project_id != SENTINEL_SHARED && db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "shared scope: project_id {:?} is neither sentinel {:?} nor a registered project",
                    project_id, SENTINEL_SHARED
                ));
            }
        }
        _ => {
            // Per-project: must reference an existing registered project.
            // Prevents projectA's modules from writing/reading secrets
            // under a project_id they make up.
            if db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "per-project scope: project {:?} is not a registered project",
                    project_id
                ));
            }
        }
    }
    Ok(())
}

#[command]
pub async fn set_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    value: String,
    validation_regex: Option<String>,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Validate value against the manifest regex if provided.
    if let Some(pattern) = validation_regex.as_deref() {
        let re = regex::Regex::new(pattern)
            .map_err(|e| format!("invalid validation regex: {}", e))?;
        if !re.is_match(&value) {
            return Err("value does not match validation pattern".into());
        }
    }

    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::set(scope_enum, &module_id, &key, &value)?;

    db.audit(
        "secret_set",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "key": key,
            "scope": scope,
            "sensitive": sensitive,
            // Never log the value, not even truncated. Presence + scope are
            // enough to reconstruct "what happened" for debugging.
        }),
    )?;
    Ok(())
}

/// Unset a secret: clear the keychain value but the UI keeps the entry
/// registered (still listed as "not set"). Useful when rotating tokens —
/// unset the old value, fetch a new one, then `set_secret_v2` the new value.
///
/// Distinct from `remove_secret_v2`, which both clears the value AND
/// signals the UI should drop the entry from its in-memory registry. At the
/// keychain layer the two are identical (no value left); the UX-level
/// distinction lives in the audit-event label and the frontend store.
#[command]
pub async fn clear_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::delete(scope_enum, &module_id, &key)?;
    db.audit(
        "secret_unset",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

/// Remove a secret entry: clear the keychain value AND signal the UI to
/// drop the entry from its in-memory registry (so it stops appearing as
/// "not set"). Backend-side this is identical to `clear_secret_v2`; the
/// difference is audit-event label + frontend semantics.
///
/// Use Remove when you no longer need the entry tracked at all (e.g. you
/// uninstalled the module that needed it). Use `clear_secret_v2` (Unset)
/// when you want the entry to remain visible so the user knows it's
/// expected and can re-set it later.
#[command]
pub async fn remove_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::delete(scope_enum, &module_id, &key)?;
    db.audit(
        "secret_remove",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

#[command]
pub async fn is_secret_set(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::is_set(scope_enum, &module_id, &key)
}

/// Return a masked preview for NON-sensitive secrets only. For sensitive
/// secrets, the caller should use `is_secret_set` and render a "••••••••"
/// placeholder in the UI without calling this command.
#[command]
pub async fn get_secret_preview(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    if sensitive {
        return Err("cannot preview sensitive secret".into());
    }
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    let val = secrets::get(scope_enum, &module_id, &key)?;
    Ok(val.map(|v| secrets::mask_preview(&v)))
}

// ─── Settings ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettingEntry {
    pub key: String,
    pub value: serde_json::Value,
}

#[command]
pub async fn get_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    db: State<'_, Db>,
) -> Result<Option<serde_json::Value>, String> {
    db.get_setting(&project_id, &module_id, &key)
}

#[command]
pub async fn set_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    value: serde_json::Value,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_setting(&project_id, &module_id, &key, &value)
}

#[command]
pub async fn list_module_settings_v2(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<Vec<SettingEntry>, String> {
    let rows = db.list_module_settings(&project_id, &module_id)?;
    Ok(rows
        .into_iter()
        .map(|(key, value)| SettingEntry { key, value })
        .collect())
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// These tests cover the scope-invariant guard rails. They don't touch the
// real OS keychain — `secrets::set/get/delete` would, but
// `enforce_scope_invariants` is a pure-DB check and can be tested with an
// in-memory SQLite.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use rusqlite::{params, Connection};
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn seed_project(db: &Db, id: &str, name: &str) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, format!("/tmp/{}", id), id, 1_700_000_000_000_i64],
            )
            .unwrap();
    }

    #[test]
    fn invariants_per_project_requires_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Registered project: passes.
        assert!(enforce_scope_invariants("per_project", "p1", &db).is_ok());

        // Unregistered project: rejected.
        let err = enforce_scope_invariants("per_project", "ghost", &db).unwrap_err();
        assert!(
            err.contains("not a registered project"),
            "expected isolation error, got: {}",
            err
        );
    }

    #[test]
    fn invariants_global_requires_sentinel() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("global", SENTINEL_GLOBAL, &db).is_ok());

        // Real project_id under global scope is rejected — global is
        // machine-wide, must not be tied to any project's scope.
        let err = enforce_scope_invariants("global", "p1", &db).unwrap_err();
        assert!(err.contains("global scope"), "got: {}", err);
    }

    #[test]
    fn invariants_shared_accepts_sentinel_or_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("shared", SENTINEL_SHARED, &db).is_ok());
        // Legacy: real project_id passes (backward compat for any
        // pre-existing project-shared entries written before this PR).
        assert!(enforce_scope_invariants("shared", "p1", &db).is_ok());
        // Unregistered project: rejected.
        assert!(enforce_scope_invariants("shared", "ghost", &db).is_err());
    }

    #[test]
    fn invariants_reject_path_traversal() {
        let db = make_db();
        for bad in [
            "",
            ".",
            "..",
            "../",
            "./foo",
            "foo/bar",
            "foo\\bar",
            "foo\0bar",
        ] {
            let err =
                enforce_scope_invariants("per_project", bad, &db).unwrap_err();
            assert!(
                err.contains("invalid project_id"),
                "expected traversal rejection for {:?}, got: {}",
                bad,
                err
            );
        }
    }

    #[test]
    fn invariants_audit_label_unset_vs_remove_is_caller_concern() {
        // Sanity-check the constants the frontend relies on.
        assert_eq!(SENTINEL_GLOBAL, "_global_");
        assert_eq!(SENTINEL_SHARED, "_user_shared_");
    }
}
