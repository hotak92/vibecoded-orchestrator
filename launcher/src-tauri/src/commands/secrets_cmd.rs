//! Tauri commands exposing secrets + settings to the React UI.
//!
//! Secret values NEVER leave the Rust process. Commands return presence
//! booleans and masked previews only. Settings are non-sensitive and are
//! returned fully.
//!
//! ─── Secret lifecycle (Bug 3 follow-up to PR #60) ──────────────────────
//!
//!  Lifecycle B (user-selected): the keychain VALUE is preserved across
//!  Unset → Reactivate cycles. The "active" state is a separate flag in
//!  `launcher.db::secret_active_state` (Storage A). Readers gate on it
//!  BEFORE returning anything from the keychain.
//!
//!  Operations:
//!    * `set_secret_v2`        — write value to keychain, mark active.
//!                               Used for both initial Set and Update.
//!    * `clear_secret_v2`      — Unset. Mark inactive. **Keychain
//!                               UNTOUCHED.** Read API will refuse to
//!                               return the value while inactive.
//!    * `reactivate_secret_v2` — flip active back to true. **No keychain
//!                               change**, no value re-entry required.
//!                               One-click resume after a rotation pause.
//!    * `remove_secret_v2`     — DELETE the keychain value AND drop the
//!                               active-state row. The entry is gone.
//!    * `is_secret_set`        — true ONLY when keychain has a value AND
//!                               active=true. Returns false for inactive.
//!    * `get_secret_preview`   — masked preview ONLY when active=true.
//!                               Returns Ok(None) for inactive, never
//!                               leaks the canary.

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
    // Setting a value implicitly activates the entry. Covers both the
    // first Set and any later Update / "Set as new value" path. Without
    // this, an entry that was Unset and then re-Set without going through
    // Reactivate would still read as inactive.
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;

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

/// Unset (Lifecycle B): flip the entry to INACTIVE without touching the
/// keychain value. The value stays in the OS keychain so a later
/// `reactivate_secret_v2` can resume the entry without the user re-typing
/// the value. While inactive, `is_secret_set` returns false and
/// `get_secret_preview` returns Ok(None) — the value cannot leak through
/// the launcher's API.
///
/// Distinct from `remove_secret_v2`, which deletes the keychain value AND
/// the active-state row.
#[command]
pub async fn clear_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Mark inactive. Do NOT call `secrets::delete` — that's the whole
    // Lifecycle B requirement. The keychain entry is the user's saved
    // value; we just gate readers on the active flag.
    db.mark_secret_inactive(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_unset",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

/// Reactivate a previously-Unset entry. Flips active=true. **Does not
/// touch the keychain.** The value that was already there is now
/// re-exposed to readers. This is the one-click "resume rotation pause"
/// path.
///
/// If the keychain has no value (e.g. a user manually deleted it via the
/// OS keychain UI while the launcher was inactive), this still flips the
/// flag — the next `is_secret_set` will simply return false because the
/// keychain side is empty. The flag itself is independent of the value's
/// existence; the read gate is `keychain_has_value AND active=true`.
#[command]
pub async fn reactivate_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_reactivate",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

/// Remove a secret entry: delete the keychain value AND drop the
/// active-state row. The entry is gone — Set requires re-typing the
/// value.
///
/// This is the destructive path. Use Unset (`clear_secret_v2`) if the
/// user just wants to pause an entry for token rotation.
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
    // Also drop the active-state row so a future Add of the same key
    // starts from a clean slate (default-active).
    db.forget_secret_active_state(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_remove",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

/// Combined status used by the secrets panel UI. `is_set` follows the
/// same gate as `is_secret_set` (true ⇔ keychain has value AND
/// active=true). `has_saved_value` reports whether the keychain still
/// has a value REGARDLESS of the active flag — the UI uses this to tell
/// "newly added, never set" (no saved value) apart from "Unset, value
/// preserved" (saved value but inactive).
///
/// `has_saved_value` is metadata about the LIFECYCLE state, not the
/// secret itself — it discloses no value bytes. The audit log mentions
/// `secret_unset` / `secret_reactivate` already, so an attacker with DB
/// read access can already reconstruct this fact; surfacing the boolean
/// to the UI does not weaken the model.
#[derive(Debug, Serialize)]
pub struct SecretStatus {
    pub is_set: bool,
    pub is_active: bool,
    pub has_saved_value: bool,
}

#[command]
pub async fn get_secret_status_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<SecretStatus, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    let active = db.is_secret_active(&scope, &project_id, &module_id, &key)?;
    let has_saved_value = secrets::is_set(scope_enum, &module_id, &key)?;
    Ok(SecretStatus {
        is_set: active && has_saved_value,
        is_active: active,
        has_saved_value,
    })
}

/// True only when the keychain has a value AND the launcher's active
/// flag is set. Returns false for inactive (Unset) entries even though
/// the keychain still has the value — that is the read-time gate.
#[command]
pub async fn is_secret_set(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Read-time gate: an inactive entry MUST appear "not set" to the UI
    // and to any module asking via this command. We check the gate FIRST
    // to avoid an unnecessary keychain round-trip when the entry is
    // paused.
    if !db.is_secret_active(&scope, &project_id, &module_id, &key)? {
        return Ok(false);
    }
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::is_set(scope_enum, &module_id, &key)
}

/// Return a masked preview for NON-sensitive secrets only. For sensitive
/// secrets, the caller should use `is_secret_set` and render a "••••••••"
/// placeholder in the UI without calling this command.
///
/// Read-time gate (Bug 3): inactive entries return Ok(None) regardless
/// of keychain state. This is the canary-test invariant — a paused
/// secret must NOT leak through the preview path even as a masked value.
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
    // Active-flag gate. Even though the keychain still holds the value
    // for an Unset entry, we treat it as if it weren't there for the
    // purposes of the public API. The user "paused" the entry; readers
    // (including the UI itself) must not see anything but a "not set"
    // signal until Reactivate.
    if !db.is_secret_active(&scope, &project_id, &module_id, &key)? {
        return Ok(None);
    }
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
// These tests cover the scope-invariant guard rails and the active-flag
// gate. The scope-invariants check is a pure-DB function; the
// active-flag tests use the in-memory DB helpers and (where the keychain
// is involved) skip via `keyring_available()`.

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
        // Placeholder folder_path string — never resolved against disk by
        // these tests. Use a platform-appropriate prefix so the value isn't
        // ambiguous on Windows.
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\{}", id)
        } else {
            format!("/tmp/{}", id)
        };
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, folder, id, 1_700_000_000_000_i64],
            )
            .unwrap();
    }

    /// Probe whether the OS keychain backend is available in this test
    /// environment. CI containers and headless build hosts typically
    /// have no Secret Service / Keychain / Credential Manager running,
    /// so any test that exercises the actual keychain has to short-circuit.
    fn keyring_available() -> bool {
        let entry = match keyring::Entry::new("vct.test.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        // Try a write+delete round-trip. Any error means the backend
        // can't be reached — we skip the keychain-touching tests.
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
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

    /// Pure-DB regression for the active-flag default. A secret with no
    /// row in `secret_active_state` is treated as ACTIVE — anything else
    /// would silently break every entry written before migration 007.
    #[test]
    fn active_flag_defaults_active_when_no_row() {
        let db = make_db();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Pure-DB regression for the unset → reactivate roundtrip.
    /// `clear_secret_v2` and `reactivate_secret_v2` both go through these
    /// `mark_secret_*` helpers, so this exercises the storage layer
    /// without needing a real keychain.
    #[test]
    fn active_flag_unset_then_reactivate_roundtrip() {
        let db = make_db();
        db.mark_secret_inactive("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(!db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
        db.mark_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Canary test (Bug 3 security requirement): an Unset entry must NOT
    /// leak the value via the preview path. We write a unique canary
    /// directly through `secrets::*` + the active-flag DB helpers, then
    /// call the gate logic the public Tauri commands use. Going through
    /// `secrets::*` rather than the wrapped `#[command]` functions lets
    /// us avoid Tauri's `State<'_, Db>` machinery in unit tests while
    /// still exercising the exact same gate.
    ///
    /// Skipped in CI environments without an OS keychain backend (most
    /// Linux build hosts). Run locally with a logged-in desktop session
    /// or pass through to a workstation pre-merge.
    #[test]
    fn inactive_secret_does_not_leak_preview() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }

        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Unique canary — substring detection catches any accidental
        // leak even if a future bug changes the masking format.
        let canary = format!(
            "test-secret-leak-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let key = format!(
            "CANARY_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let scope = "per_project";
        let project_id = "p1";
        let module_id = "user";

        // Set + activate (mirrors `set_secret_v2`).
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).expect("keychain set");
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("mark active");

        // Sanity: while ACTIVE the gate lets a masked preview through.
        // (We replicate `get_secret_preview`'s body since the
        // `#[command]` wrapper requires Tauri State.)
        let preview_active = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(preview_active.is_some(), "preview missing while active");
        // The masked form must NOT contain the raw canary verbatim.
        assert!(
            !preview_active.as_ref().unwrap().contains(&canary),
            "raw canary leaked through the masked preview while active"
        );

        // Unset — the Lifecycle B path. KEYCHAIN UNTOUCHED.
        db.mark_secret_inactive(scope, project_id, module_id, &key)
            .expect("mark inactive");

        // The keychain still has the value (proves Lifecycle B):
        let kc = secrets::get(scope_enum, module_id, &key).unwrap();
        assert_eq!(
            kc.as_deref(),
            Some(canary.as_str()),
            "Lifecycle B violated: unset cleared the keychain"
        );

        // But the read gate must lie about it:
        let is_set_inactive = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            !is_set_inactive,
            "is_secret_set leaked an inactive entry as set"
        );

        // And the preview gate MUST return None (not even a masked form):
        let preview_inactive = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(
            preview_inactive.is_none(),
            "preview leaked while inactive: {:?}",
            preview_inactive
        );
        if let Some(p) = preview_inactive.as_ref() {
            assert!(!p.contains(&canary), "canary substring leaked");
        }

        // Reactivate — flips flag, keychain unchanged.
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("reactivate");

        // The same gate now yields the value again WITHOUT having
        // re-typed it. This is the user-facing benefit of Lifecycle B.
        let is_set_after = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            is_set_after,
            "reactivate did not restore the read gate; user would have to re-enter the value"
        );

        // Cleanup keychain (best-effort).
        let _ = secrets::delete(scope_enum, module_id, &key);
        let _ = db.forget_secret_active_state(scope, project_id, module_id, &key);
    }
}
