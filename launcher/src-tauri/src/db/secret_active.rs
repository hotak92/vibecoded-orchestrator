//! Per-secret active flag — Storage A in the Bug 3 follow-up to PR #60.
//!
//! Secrets live in the OS keychain (see `crate::secrets`). This DB table
//! holds whether a registered secret is currently ACTIVE (readers may
//! receive its value) or INACTIVE (the value stays in the keychain so a
//! later one-click reactivation works without re-entry, but readers are
//! gated as if the secret were not set).
//!
//! Read-time gate is enforced in `commands/secrets_cmd.rs::is_secret_set`
//! and `get_secret_preview`. Both consult `is_active(...)` BEFORE
//! returning data — never trust the keychain alone.
//!
//! Default semantics: a secret with NO row in this table is ACTIVE. The
//! first `set_secret_v2` for a key writes `active=1` explicitly. `Unset`
//! writes `active=0`. `Reactivate` writes `active=1`. `Remove` deletes
//! the row.

use rusqlite::params;

use super::Db;

impl Db {
    /// Read the active flag for a (scope, project_id, module_id, key)
    /// tuple. Returns `true` when no row exists (default-active for any
    /// secret that pre-existed migration 007 or was just registered).
    pub fn is_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let row: Option<i64> = guard
            .query_row(
                "SELECT active FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3 AND key = ?4",
                params![scope, project_id, module_id, key],
                |r| r.get(0),
            )
            .ok();
        Ok(row.map(|v| v != 0).unwrap_or(true))
    }

    /// Mark a secret active. Idempotent — safe to call from `set` and
    /// `reactivate` paths alike. Stamps `updated_at` to now.
    pub fn mark_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state (scope, project_id, module_id, key, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 1, ?5)
                 ON CONFLICT(scope, project_id, module_id, key)
                 DO UPDATE SET active = 1, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, now],
            )
            .map_err(|e| format!("mark_secret_active: {}", e))?;
        Ok(())
    }

    /// Mark a secret inactive. The keychain value is NOT touched — that is
    /// the whole point of Lifecycle B (Bug 3 fix). After this call, the
    /// public read API (`is_secret_set`, `get_secret_preview`) MUST refuse
    /// to surface the value.
    pub fn mark_secret_inactive(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state (scope, project_id, module_id, key, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 0, ?5)
                 ON CONFLICT(scope, project_id, module_id, key)
                 DO UPDATE SET active = 0, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, now],
            )
            .map_err(|e| format!("mark_secret_inactive: {}", e))?;
        Ok(())
    }

    /// Drop the active-state row entirely. Used by Remove (the entry no
    /// longer exists, so any "active" metadata for it is stale). Idempotent.
    pub fn forget_secret_active_state(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3 AND key = ?4",
                params![scope, project_id, module_id, key],
            )
            .map_err(|e| format!("forget_secret_active_state: {}", e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_active_when_no_row() {
        let db = Db::open_in_memory().unwrap();
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }

    #[test]
    fn mark_inactive_then_reactivate_roundtrip() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("global", "_global_", "u", "K").unwrap();
        assert!(!db.is_secret_active("global", "_global_", "u", "K").unwrap());
        db.mark_secret_active("global", "_global_", "u", "K").unwrap();
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }

    #[test]
    fn forget_resets_to_default() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("global", "_global_", "u", "K").unwrap();
        db.forget_secret_active_state("global", "_global_", "u", "K").unwrap();
        // After forget, default-active applies again.
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }
}
