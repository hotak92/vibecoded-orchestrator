//! Row-level CRUD for `module_installs`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::{ModuleInstallRow, ModuleStatus};
use super::Db;

impl Db {
    pub fn insert_module_install(
        &self,
        id: &str,
        project_id: &str,
        module_id: &str,
        module_version: &str,
        install_path: &str,
    ) -> Result<ModuleInstallRow, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_installs
                 (id, project_id, module_id, module_version, install_path,
                  status, enabled, installed_at, last_started_at, last_error)
                 VALUES (?1, ?2, ?3, ?4, ?5, 'installing', 1, ?6, NULL, NULL)",
                params![id, project_id, module_id, module_version, install_path, now],
            )
            .map_err(|e| format!("insert module_install: {}", e))?;
        Ok(ModuleInstallRow {
            id: id.to_string(),
            project_id: project_id.to_string(),
            module_id: module_id.to_string(),
            module_version: module_version.to_string(),
            install_path: install_path.to_string(),
            status: ModuleStatus::Installing,
            enabled: true,
            installed_at: now,
            last_started_at: None,
            last_error: None,
        })
    }

    pub fn set_module_status(
        &self,
        project_id: &str,
        module_id: &str,
        status: ModuleStatus,
        error: Option<String>,
    ) -> Result<(), String> {
        let guard = self.lock();
        let started_at = if status == ModuleStatus::Running {
            Some(Utc::now().timestamp_millis())
        } else {
            None
        };
        guard
            .execute(
                "UPDATE module_installs
                    SET status = ?1,
                        last_error = ?2,
                        last_started_at = COALESCE(?3, last_started_at)
                  WHERE project_id = ?4 AND module_id = ?5",
                params![status.as_str(), error, started_at, project_id, module_id],
            )
            .map_err(|e| format!("set status: {}", e))?;
        Ok(())
    }

    pub fn set_module_enabled(
        &self,
        project_id: &str,
        module_id: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "UPDATE module_installs SET enabled = ?1
                  WHERE project_id = ?2 AND module_id = ?3",
                params![enabled as i32, project_id, module_id],
            )
            .map_err(|e| format!("set enabled: {}", e))?;
        Ok(())
    }

    pub fn get_module_install(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Option<ModuleInstallRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error
                 FROM module_installs
                 WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
                |row| {
                    let status_s: String = row.get(5)?;
                    let enabled_i: i32 = row.get(6)?;
                    Ok(ModuleInstallRow {
                        id: row.get(0)?,
                        project_id: row.get(1)?,
                        module_id: row.get(2)?,
                        module_version: row.get(3)?,
                        install_path: row.get(4)?,
                        status: ModuleStatus::from_str(&status_s).unwrap_or(ModuleStatus::Error),
                        enabled: enabled_i != 0,
                        installed_at: row.get(7)?,
                        last_started_at: row.get(8)?,
                        last_error: row.get(9)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("get module_install: {}", e))
    }

    pub fn list_module_installs_for_project(
        &self,
        project_id: &str,
    ) -> Result<Vec<ModuleInstallRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error
                 FROM module_installs WHERE project_id = ?1 ORDER BY installed_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |row| {
                let status_s: String = row.get(5)?;
                let enabled_i: i32 = row.get(6)?;
                Ok(ModuleInstallRow {
                    id: row.get(0)?,
                    project_id: row.get(1)?,
                    module_id: row.get(2)?,
                    module_version: row.get(3)?,
                    install_path: row.get(4)?,
                    status: ModuleStatus::from_str(&status_s).unwrap_or(ModuleStatus::Error),
                    enabled: enabled_i != 0,
                    installed_at: row.get(7)?,
                    last_started_at: row.get(8)?,
                    last_error: row.get(9)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn delete_module_install(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_installs WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
            )
            .map_err(|e| format!("delete: {}", e))?;
        Ok(())
    }
}
