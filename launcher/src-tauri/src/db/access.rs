//! Access-matrix CRUD: KG collections + cross-project codegraph.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::Db;

// ─── KG collection access ────────────────────────────────────────────────

impl Db {
    pub fn kg_get_access(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<Option<String>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, collection],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("kg_get_access: {}", e))
    }

    pub fn kg_set_access(
        &self,
        project_id: &str,
        collection: &str,
        level: &str,
    ) -> Result<(), String> {
        if !matches!(level, "read" | "write" | "none") {
            return Err(format!("invalid kg access level: {}", level));
        }
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO kg_collection_access (project_id, collection_name, access_level)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(project_id, collection_name)
                 DO UPDATE SET access_level = excluded.access_level",
                params![project_id, collection, level],
            )
            .map_err(|e| format!("kg_set_access: {}", e))?;
        Ok(())
    }

    pub fn kg_list_access(
        &self,
        project_id: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT collection_name, access_level FROM kg_collection_access
                  WHERE project_id = ?1 ORDER BY collection_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }
}

// ─── Codegraph access ────────────────────────────────────────────────────

impl Db {
    pub fn codegraph_grant(
        &self,
        grantor: &str,
        grantee: &str,
        level: &str,
    ) -> Result<(), String> {
        if !matches!(level, "read" | "none") {
            return Err(format!("invalid codegraph access level: {}", level));
        }
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO codegraph_access
                 (grantor_project_id, grantee_project_id, access_level, granted_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(grantor_project_id, grantee_project_id)
                 DO UPDATE SET access_level = excluded.access_level,
                               granted_at = excluded.granted_at",
                params![grantor, grantee, level, Utc::now().timestamp_millis()],
            )
            .map_err(|e| format!("codegraph_grant: {}", e))?;
        Ok(())
    }

    pub fn codegraph_check(
        &self,
        grantor: &str,
        grantee: &str,
    ) -> Result<Option<String>, String> {
        if grantor == grantee {
            return Ok(Some("read".to_string()));
        }
        let guard = self.lock();
        guard
            .query_row(
                "SELECT access_level FROM codegraph_access
                  WHERE grantor_project_id = ?1 AND grantee_project_id = ?2",
                params![grantor, grantee],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("codegraph_check: {}", e))
    }

    pub fn codegraph_list_grants_from(
        &self,
        grantor: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT grantee_project_id, access_level FROM codegraph_access
                  WHERE grantor_project_id = ?1 ORDER BY granted_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantor], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn codegraph_list_grants_to(
        &self,
        grantee: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT grantor_project_id, access_level FROM codegraph_access
                  WHERE grantee_project_id = ?1 ORDER BY granted_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantee], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }
}

// ─── Audit log ───────────────────────────────────────────────────────────

impl Db {
    pub fn audit(
        &self,
        operation: &str,
        project_id: Option<&str>,
        module_id: Option<&str>,
        detail: &serde_json::Value,
    ) -> Result<(), String> {
        self.audit_as(super::current_actor(), operation, project_id, module_id, detail)
    }

    /// Variant that lets the caller override the actor — useful for
    /// commands that act on behalf of a specific user (e.g. when the
    /// launcher gains real auth). Today everything goes through `audit`
    /// which uses `current_actor()`.
    pub fn audit_as(
        &self,
        actor: &str,
        operation: &str,
        project_id: Option<&str>,
        module_id: Option<&str>,
        detail: &serde_json::Value,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO audit_log (operation, project_id, module_id, detail, actor, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    operation,
                    project_id,
                    module_id,
                    detail.to_string(),
                    actor,
                    Utc::now().timestamp_millis(),
                ],
            )
            .map_err(|e| format!("audit: {}", e))?;
        Ok(())
    }

    /// Read audit entries, newest first. Optionally filter by project_id.
    /// `limit` is bounded to 1000 to keep payloads small.
    pub fn audit_list(
        &self,
        project_id: Option<&str>,
        limit: u32,
    ) -> Result<Vec<crate::commands::audit::AuditEvent>, String> {
        let guard = self.lock();
        let limit = std::cmp::min(limit, 1000);

        let (sql, has_filter) = if project_id.is_some() {
            (
                "SELECT id, operation, project_id, module_id, detail, actor, created_at
                 FROM audit_log
                 WHERE project_id = ?1
                 ORDER BY created_at DESC
                 LIMIT ?2",
                true,
            )
        } else {
            (
                "SELECT id, operation, project_id, module_id, detail, actor, created_at
                 FROM audit_log
                 ORDER BY created_at DESC
                 LIMIT ?1",
                false,
            )
        };

        let mut stmt = guard.prepare(sql).map_err(|e| format!("audit_list prepare: {}", e))?;

        let map_row = |row: &rusqlite::Row| -> rusqlite::Result<crate::commands::audit::AuditEvent> {
            Ok(crate::commands::audit::AuditEvent {
                id: row.get(0)?,
                operation: row.get(1)?,
                project_id: row.get(2)?,
                module_id: row.get(3)?,
                detail: row.get(4)?,
                actor: row.get(5)?,
                created_at: row.get(6)?,
            })
        };

        let rows: Vec<_> = if has_filter {
            stmt.query_map(params![project_id.unwrap(), limit], map_row)
                .map_err(|e| format!("audit_list query: {}", e))?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("audit_list collect: {}", e))?
        } else {
            stmt.query_map(params![limit], map_row)
                .map_err(|e| format!("audit_list query: {}", e))?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("audit_list collect: {}", e))?
        };

        Ok(rows)
    }
}
