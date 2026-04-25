//! Row-level CRUD for `projects` table. Higher-level logic (host switching
//! with module uninstalls, validation, audit logging) lives in
//! `crate::commands::projects_v2`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::{ProjectHost, ProjectRow};
use super::slug::{slugify, unique_slug};
use super::Db;

impl Db {
    /// Generate a unique slug for the given project name. Pure helper —
    /// does NOT modify any rows. Caller passes the result to `insert_project`.
    pub fn generate_unique_slug(&self, name: &str) -> Result<String, String> {
        let base = slugify(name);
        let guard = self.lock();
        let mut stmt = guard
            .prepare("SELECT 1 FROM projects WHERE slug = ?1 LIMIT 1")
            .map_err(|e| format!("prepare slug check: {}", e))?;

        let mut taken = |candidate: &str| -> bool {
            stmt.query_row(params![candidate], |_| Ok(()))
                .optional()
                .map(|o| o.is_some())
                .unwrap_or(false)
        };
        Ok(unique_slug(&base, |c| taken(c)))
    }

    pub fn insert_project(
        &self,
        id: &str,
        name: &str,
        folder_path: &str,
        host: ProjectHost,
        slug: &str,
    ) -> Result<ProjectRow, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6)",
                params![id, name, folder_path, host.as_str(), slug, now],
            )
            .map_err(|e| format!("insert project: {}", e))?;
        Ok(ProjectRow {
            id: id.to_string(),
            name: name.to_string(),
            folder_path: folder_path.to_string(),
            host,
            slug: slug.to_string(),
            created_at: now,
            updated_at: now,
        })
    }

    pub fn get_project(&self, id: &str) -> Result<Option<ProjectRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, name, folder_path, host, slug, created_at, updated_at
                 FROM projects WHERE id = ?1",
                params![id],
                row_to_project,
            )
            .optional()
            .map_err(|e| format!("get project: {}", e))
    }

    /// Look up a project by URL slug (e.g. `"acme-corp"`). Returns `None`
    /// if no row matches; the slug column has a UNIQUE index so at most
    /// one row can match.
    pub fn get_project_by_slug(&self, slug: &str) -> Result<Option<ProjectRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, name, folder_path, host, slug, created_at, updated_at
                 FROM projects WHERE slug = ?1",
                params![slug],
                row_to_project,
            )
            .optional()
            .map_err(|e| format!("get project by slug: {}", e))
    }

    pub fn list_projects(&self) -> Result<Vec<ProjectRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, name, folder_path, host, slug, created_at, updated_at
                 FROM projects ORDER BY name ASC",
            )
            .map_err(|e| format!("prepare list: {}", e))?;
        let rows = stmt
            .query_map([], row_to_project)
            .map_err(|e| format!("query list: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list: {}", e))
    }

    /// Rename + regenerate slug if requested. The slug parameter, when
    /// `Some`, is used verbatim — caller is responsible for uniqueness
    /// (use `generate_unique_slug` first). When `None`, slug is left
    /// untouched (legacy callers).
    pub fn rename_project(
        &self,
        id: &str,
        new_name: &str,
        new_slug: Option<&str>,
    ) -> Result<(), String> {
        let guard = self.lock();
        let now = Utc::now().timestamp_millis();
        let n = if let Some(s) = new_slug {
            guard
                .execute(
                    "UPDATE projects SET name = ?1, slug = ?2, updated_at = ?3 WHERE id = ?4",
                    params![new_name, s, now, id],
                )
                .map_err(|e| format!("rename: {}", e))?
        } else {
            guard
                .execute(
                    "UPDATE projects SET name = ?1, updated_at = ?2 WHERE id = ?3",
                    params![new_name, now, id],
                )
                .map_err(|e| format!("rename: {}", e))?
        };
        if n == 0 {
            return Err(format!("project {} not found", id));
        }
        Ok(())
    }

    pub fn update_project_host(&self, id: &str, new_host: ProjectHost) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE projects SET host = ?1, updated_at = ?2 WHERE id = ?3",
                params![new_host.as_str(), Utc::now().timestamp_millis(), id],
            )
            .map_err(|e| format!("update host: {}", e))?;
        if n == 0 {
            return Err(format!("project {} not found", id));
        }
        Ok(())
    }

    pub fn delete_project(&self, id: &str) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute("DELETE FROM projects WHERE id = ?1", params![id])
            .map_err(|e| format!("delete: {}", e))?;
        Ok(())
    }
}

fn row_to_project(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProjectRow> {
    let host_s: String = row.get(3)?;
    Ok(ProjectRow {
        id: row.get(0)?,
        name: row.get(1)?,
        folder_path: row.get(2)?,
        host: ProjectHost::from_str(&host_s).unwrap_or(ProjectHost::Base),
        slug: row.get(4)?,
        created_at: row.get(5)?,
        updated_at: row.get(6)?,
    })
}
