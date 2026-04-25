//! Row-level CRUD for `projects` table. Higher-level logic (host switching
//! with module uninstalls, validation, audit logging) lives in
//! `crate::commands::projects_v2`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::{ProjectHost, ProjectRow};
use super::Db;

impl Db {
    pub fn insert_project(
        &self,
        id: &str,
        name: &str,
        folder_path: &str,
        host: ProjectHost,
    ) -> Result<ProjectRow, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?5)",
                params![id, name, folder_path, host.as_str(), now],
            )
            .map_err(|e| format!("insert project: {}", e))?;
        Ok(ProjectRow {
            id: id.to_string(),
            name: name.to_string(),
            folder_path: folder_path.to_string(),
            host,
            created_at: now,
            updated_at: now,
        })
    }

    pub fn get_project(&self, id: &str) -> Result<Option<ProjectRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, name, folder_path, host, created_at, updated_at
                 FROM projects WHERE id = ?1",
                params![id],
                |row| {
                    let host_s: String = row.get(3)?;
                    Ok(ProjectRow {
                        id: row.get(0)?,
                        name: row.get(1)?,
                        folder_path: row.get(2)?,
                        host: ProjectHost::from_str(&host_s).unwrap_or(ProjectHost::Base),
                        created_at: row.get(4)?,
                        updated_at: row.get(5)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("get project: {}", e))
    }

    pub fn list_projects(&self) -> Result<Vec<ProjectRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, name, folder_path, host, created_at, updated_at
                 FROM projects ORDER BY name ASC",
            )
            .map_err(|e| format!("prepare list: {}", e))?;
        let rows = stmt
            .query_map([], |row| {
                let host_s: String = row.get(3)?;
                Ok(ProjectRow {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    folder_path: row.get(2)?,
                    host: ProjectHost::from_str(&host_s).unwrap_or(ProjectHost::Base),
                    created_at: row.get(4)?,
                    updated_at: row.get(5)?,
                })
            })
            .map_err(|e| format!("query list: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list: {}", e))
    }

    pub fn rename_project(&self, id: &str, new_name: &str) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE projects SET name = ?1, updated_at = ?2 WHERE id = ?3",
                params![new_name, Utc::now().timestamp_millis(), id],
            )
            .map_err(|e| format!("rename: {}", e))?;
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
