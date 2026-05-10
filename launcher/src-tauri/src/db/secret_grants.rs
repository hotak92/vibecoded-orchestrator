//! Per-project secret grants — 0.2.1 (migration 009).
//!
//! Maps `(owner-project secret)` → `(grantee-project read access)`.
//! The hub resolver's "can project P read this secret?" check expands
//! from `P == owner_project_id` to
//!   `P == owner OR exists row in secret_grants(owner, P)`.
//!
//! Only the OWNER can INSERT or DELETE grants here (enforced at the
//! Tauri command layer, not the DB). The grantee can opt itself out
//! via `secret_active_state` (per-requester pause); the grant row
//! stays so the launcher GUI shows "B has paused this grant" without
//! losing the relationship.
//!
//! Scope is currently `'per_project'` only — granting global or
//! shared secrets is meaningless because they're already cross-
//! project. The schema's CHECK leaves room for a future extension.

use rusqlite::params;

use super::Db;

/// One row in `secret_grants`. Columns mirror migration 009 verbatim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretGrant {
    pub scope: String,
    pub owner_project_id: String,
    pub module_id: String,
    pub key: String,
    pub grantee_project_id: String,
    pub granted_at: i64,
    pub granted_by_actor: Option<String>,
    pub note: Option<String>,
}

impl Db {
    /// Insert a grant. Idempotent — a duplicate grant is a no-op
    /// (`ON CONFLICT DO NOTHING`) so the GUI's "Grant to project…"
    /// action can be retried without surprising the user.
    ///
    /// Returns `true` if a new row was actually inserted (not a
    /// duplicate). Useful for the audit log so we don't emit a
    /// "granted" entry every time the user clicks the same checkbox.
    pub fn insert_secret_grant(
        &self,
        scope: &str,
        owner_project_id: &str,
        module_id: &str,
        key: &str,
        grantee_project_id: &str,
        granted_by_actor: Option<&str>,
        note: Option<&str>,
    ) -> Result<bool, String> {
        if owner_project_id == grantee_project_id {
            return Err("insert_secret_grant: owner and grantee must differ".to_string());
        }
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "INSERT INTO secret_grants
                    (scope, owner_project_id, module_id, key, grantee_project_id,
                     granted_at, granted_by_actor, note)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                 ON CONFLICT(scope, owner_project_id, module_id, key, grantee_project_id)
                 DO NOTHING",
                params![
                    scope,
                    owner_project_id,
                    module_id,
                    key,
                    grantee_project_id,
                    now,
                    granted_by_actor,
                    note
                ],
            )
            .map_err(|e| format!("insert_secret_grant: {}", e))?;
        Ok(n > 0)
    }

    /// Revoke a grant. Idempotent — deleting an already-absent row
    /// returns `Ok(false)` (no row deleted). Only the owner should
    /// reach this code path; enforcement lives at the Tauri command
    /// layer, not in SQL.
    pub fn revoke_secret_grant(
        &self,
        scope: &str,
        owner_project_id: &str,
        module_id: &str,
        key: &str,
        grantee_project_id: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "DELETE FROM secret_grants
                  WHERE scope = ?1 AND owner_project_id = ?2 AND module_id = ?3
                    AND key = ?4 AND grantee_project_id = ?5",
                params![scope, owner_project_id, module_id, key, grantee_project_id],
            )
            .map_err(|e| format!("revoke_secret_grant: {}", e))?;
        Ok(n > 0)
    }

    /// True if `grantee_project_id` has a grant for the secret. Used
    /// by the resolver's "can this project read it?" check —
    /// resolver also accepts `grantee == owner` directly without
    /// consulting this table.
    pub fn has_secret_grant(
        &self,
        scope: &str,
        owner_project_id: &str,
        module_id: &str,
        key: &str,
        grantee_project_id: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let row: Option<i64> = guard
            .query_row(
                "SELECT 1 FROM secret_grants
                  WHERE scope = ?1 AND owner_project_id = ?2 AND module_id = ?3
                    AND key = ?4 AND grantee_project_id = ?5",
                params![scope, owner_project_id, module_id, key, grantee_project_id],
                |r| r.get(0),
            )
            .ok();
        Ok(row.is_some())
    }

    /// Enumerate every grant the owner project has issued. Used by
    /// the launcher GUI's "Per-project" tab to render the grant grid
    /// next to each secret. Order: by `(module_id, key,
    /// grantee_project_id)` for stable rendering.
    pub fn list_grants_by_owner(&self, owner_project_id: &str) -> Result<Vec<SecretGrant>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT scope, owner_project_id, module_id, key, grantee_project_id,
                        granted_at, granted_by_actor, note
                   FROM secret_grants
                  WHERE owner_project_id = ?1
                  ORDER BY module_id ASC, key ASC, grantee_project_id ASC",
            )
            .map_err(|e| format!("list_grants_by_owner prepare: {}", e))?;
        let rows = stmt
            .query_map(params![owner_project_id], |r| {
                Ok(SecretGrant {
                    scope: r.get(0)?,
                    owner_project_id: r.get(1)?,
                    module_id: r.get(2)?,
                    key: r.get(3)?,
                    grantee_project_id: r.get(4)?,
                    granted_at: r.get(5)?,
                    granted_by_actor: r.get(6)?,
                    note: r.get(7)?,
                })
            })
            .map_err(|e| format!("list_grants_by_owner query: {}", e))?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(|e| format!("list_grants_by_owner row: {}", e))?);
        }
        Ok(out)
    }

    /// Enumerate every grant the grantee project has received. Used
    /// by the launcher GUI's "Shared" tab to surface grants alongside
    /// SENTINEL_SHARED secrets, and by the hub resolver to drive the
    /// "extra secrets visible to this project" join.
    pub fn list_grants_by_grantee(
        &self,
        grantee_project_id: &str,
    ) -> Result<Vec<SecretGrant>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT scope, owner_project_id, module_id, key, grantee_project_id,
                        granted_at, granted_by_actor, note
                   FROM secret_grants
                  WHERE grantee_project_id = ?1
                  ORDER BY owner_project_id ASC, module_id ASC, key ASC",
            )
            .map_err(|e| format!("list_grants_by_grantee prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantee_project_id], |r| {
                Ok(SecretGrant {
                    scope: r.get(0)?,
                    owner_project_id: r.get(1)?,
                    module_id: r.get(2)?,
                    key: r.get(3)?,
                    grantee_project_id: r.get(4)?,
                    granted_at: r.get(5)?,
                    granted_by_actor: r.get(6)?,
                    note: r.get(7)?,
                })
            })
            .map_err(|e| format!("list_grants_by_grantee query: {}", e))?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(|e| format!("list_grants_by_grantee row: {}", e))?);
        }
        Ok(out)
    }

    /// Drop every grant referencing `project_id` on either side
    /// (owner OR grantee). Used by `delete_project_v2` so an
    /// unregistered project doesn't leave dangling grants in the
    /// table. Returns total rows removed.
    pub fn forget_grants_for_project(&self, project_id: &str) -> Result<usize, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "DELETE FROM secret_grants
                  WHERE owner_project_id = ?1 OR grantee_project_id = ?1",
                params![project_id],
            )
            .map_err(|e| format!("forget_grants_for_project: {}", e))?;
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_then_has_grant() {
        let db = Db::open_in_memory().unwrap();
        let inserted = db
            .insert_secret_grant("per_project", "A", "u", "K", "B", Some("martino"), None)
            .unwrap();
        assert!(inserted);
        assert!(db
            .has_secret_grant("per_project", "A", "u", "K", "B")
            .unwrap());
    }

    #[test]
    fn duplicate_grant_is_idempotent() {
        let db = Db::open_in_memory().unwrap();
        let first = db
            .insert_secret_grant("per_project", "A", "u", "K", "B", None, None)
            .unwrap();
        let second = db
            .insert_secret_grant("per_project", "A", "u", "K", "B", None, None)
            .unwrap();
        assert!(first, "first insert reports new row");
        assert!(!second, "duplicate insert reports no-op");
    }

    #[test]
    fn owner_grantee_must_differ() {
        let db = Db::open_in_memory().unwrap();
        let err = db
            .insert_secret_grant("per_project", "A", "u", "K", "A", None, None)
            .unwrap_err();
        assert!(err.contains("must differ"));
    }

    #[test]
    fn revoke_removes_grant() {
        let db = Db::open_in_memory().unwrap();
        db.insert_secret_grant("per_project", "A", "u", "K", "B", None, None)
            .unwrap();
        let removed = db
            .revoke_secret_grant("per_project", "A", "u", "K", "B")
            .unwrap();
        assert!(removed);
        assert!(!db
            .has_secret_grant("per_project", "A", "u", "K", "B")
            .unwrap());
    }

    #[test]
    fn revoke_absent_grant_is_noop() {
        let db = Db::open_in_memory().unwrap();
        let removed = db
            .revoke_secret_grant("per_project", "A", "u", "K", "B")
            .unwrap();
        assert!(!removed);
    }

    #[test]
    fn list_by_owner_and_grantee() {
        let db = Db::open_in_memory().unwrap();
        db.insert_secret_grant("per_project", "A", "u", "K1", "B", None, None)
            .unwrap();
        db.insert_secret_grant("per_project", "A", "u", "K2", "C", None, None)
            .unwrap();
        db.insert_secret_grant("per_project", "X", "u", "K", "B", None, None)
            .unwrap();

        let by_owner = db.list_grants_by_owner("A").unwrap();
        assert_eq!(by_owner.len(), 2);
        assert_eq!(by_owner[0].key, "K1");
        assert_eq!(by_owner[1].key, "K2");

        let by_grantee = db.list_grants_by_grantee("B").unwrap();
        assert_eq!(by_grantee.len(), 2);
        // Sorted by owner_project_id ASC: A before X.
        assert_eq!(by_grantee[0].owner_project_id, "A");
        assert_eq!(by_grantee[1].owner_project_id, "X");
    }

    #[test]
    fn forget_grants_for_project_removes_both_sides() {
        let db = Db::open_in_memory().unwrap();
        // A grants to B
        db.insert_secret_grant("per_project", "A", "u", "K1", "B", None, None)
            .unwrap();
        // X grants to A
        db.insert_secret_grant("per_project", "X", "u", "K2", "A", None, None)
            .unwrap();
        // X grants to B (should survive)
        db.insert_secret_grant("per_project", "X", "u", "K3", "B", None, None)
            .unwrap();

        let n = db.forget_grants_for_project("A").unwrap();
        assert_eq!(n, 2, "both A's grants and grants TO A must go");

        // Grants not touching A remain.
        assert!(db
            .has_secret_grant("per_project", "X", "u", "K3", "B")
            .unwrap());
    }
}
