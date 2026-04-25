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

/// Map an audit operation name to the table the frontend should
/// re-fetch. Returns None for ops that already imply audit_log alone
/// (which is always logged separately). Best-effort — unknown op names
/// fall through to a generic "audit_log" event so consumers always get
/// SOME signal.
fn infer_table_for_op(op: &str) -> Option<&'static str> {
    match op {
        "project_create" | "project_delete" | "project_host_switch" | "project_rename" => {
            Some("projects")
        }
        s if s.starts_with("module_") => Some("module_installs"),
        s if s.starts_with("secret_") => Some("project_secret_refs"),
        s if s.starts_with("setting_") => Some("module_settings"),
        s if s.starts_with("kg_") => Some("kg_collection_access"),
        s if s.starts_with("codegraph_") => Some("codegraph_access"),
        s if s.starts_with("hook_") => Some("project_hooks"),
        s if s.starts_with("agent_") => Some("project_agents"),
        s if s.starts_with("skill_") => Some("project_skills"),
        s if s.starts_with("permission_") => Some("project_permissions"),
        s if s.starts_with("license_") => Some("tier_cache"),
        _ => None,
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
        {
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
        }

        // Mirror every audited mutation into the change_log so frontend
        // polling can detect cross-window edits (multi-tenant infra P7).
        let _ = self.log_change("audit_log", "insert", None, project_id);
        let inferred = infer_table_for_op(operation);
        if let Some(t) = inferred {
            let _ = self.log_change(t, "update", module_id, project_id);
        }
        Ok(())
    }

    /// Read audit entries, newest first.
    ///
    /// All filters are pushed into SQLite via parameterized WHERE clauses so
    /// we don't ship large windows to the frontend just for it to throw rows
    /// away. Earlier revisions returned up to 500 rows and let the browser
    /// filter on time-range/actor/search; that fell over for high-volume
    /// audit logs.
    ///
    /// Filter semantics:
    ///   * `project_id` — exact match on `project_id` column.
    ///   * `actor` — exact match on `actor` column (case-sensitive).
    ///   * `since_ms` / `until_ms` — inclusive bounds on `created_at`
    ///     (epoch ms). Either or both may be `None`.
    ///   * `search` — substring match (`LIKE '%' || ? || '%'`) against
    ///     `operation` OR `detail`. SQLite's default LIKE is
    ///     case-insensitive for ASCII, which matches the previous
    ///     browser-side `.toLowerCase().includes(...)` behaviour for the
    ///     ASCII range we care about.
    ///
    /// `limit` is bounded to 10000 server-side. The full table scan over a
    /// limit of that size is bounded and acceptable; we'd add a covering
    /// index if a profile ever showed it mattered.
    pub fn audit_list(
        &self,
        project_id: Option<&str>,
        actor: Option<&str>,
        since_ms: Option<i64>,
        until_ms: Option<i64>,
        search: Option<&str>,
        limit: u32,
    ) -> Result<Vec<crate::commands::audit::AuditEvent>, String> {
        let guard = self.lock();
        let limit = std::cmp::min(limit, 10000);

        // Build the WHERE clause + bound params dynamically. Using
        // `Vec<Box<dyn ToSql>>` keeps the param order tied to placeholder
        // order regardless of which filters are active.
        let mut where_parts: Vec<&'static str> = Vec::new();
        let mut bound: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(pid) = project_id {
            where_parts.push("project_id = ?");
            bound.push(Box::new(pid.to_string()));
        }
        if let Some(a) = actor {
            if !a.is_empty() {
                where_parts.push("actor = ?");
                bound.push(Box::new(a.to_string()));
            }
        }
        if let Some(s) = since_ms {
            where_parts.push("created_at >= ?");
            bound.push(Box::new(s));
        }
        if let Some(u) = until_ms {
            where_parts.push("created_at <= ?");
            bound.push(Box::new(u));
        }
        if let Some(q) = search {
            let q = q.trim();
            if !q.is_empty() {
                // Match operation OR detail. We bind the same value twice
                // (once per `?`) — clearer than reusing one `?N` and works
                // identically in SQLite.
                where_parts.push("(operation LIKE '%' || ? || '%' OR detail LIKE '%' || ? || '%')");
                bound.push(Box::new(q.to_string()));
                bound.push(Box::new(q.to_string()));
            }
        }

        let where_clause = if where_parts.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", where_parts.join(" AND "))
        };

        let sql = format!(
            "SELECT id, operation, project_id, module_id, detail, actor, created_at
             FROM audit_log{}
             ORDER BY created_at DESC
             LIMIT ?",
            where_clause
        );
        bound.push(Box::new(limit));

        let mut stmt = guard.prepare(&sql).map_err(|e| format!("audit_list prepare: {}", e))?;

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

        let param_refs: Vec<&dyn rusqlite::ToSql> = bound.iter().map(|b| &**b as &dyn rusqlite::ToSql).collect();

        let rows: Vec<_> = stmt
            .query_map(rusqlite::params_from_iter(param_refs.iter()), map_row)
            .map_err(|e| format!("audit_list query: {}", e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("audit_list collect: {}", e))?;

        Ok(rows)
    }
}
