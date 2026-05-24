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
            container_name: None,
        })
    }

    /// Persist a resolved container name on a module_install row
    /// (migration 015, Phase 1E). HUB-only writer (see B2 single-writer
    /// principle in models.rs::ModuleInstallRow::container_name doc).
    /// Called by `vct-hub::module_supervisor::start_container_for_module`
    /// immediately after `podman run` succeeds, so the launcher's startup
    /// hook + uninstall path can enumerate per-project containers.
    pub fn set_module_container_name(
        &self,
        project_id: &str,
        module_id: &str,
        container_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs SET container_name = ?1
                  WHERE project_id = ?2 AND module_id = ?3",
                params![container_name, project_id, module_id],
            )
            .map_err(|e| format!("set container_name: {}", e))?;
        if n == 0 {
            return Err(format!(
                "module_install not found for project={} module={}",
                project_id, module_id
            ));
        }
        Ok(())
    }

    /// List every (project_id, module_id, container_name) triple where
    /// container_name is non-null. Used by the launcher's startup hook
    /// to enumerate per-project containers that need re-checking after
    /// a quit-relaunch cycle.
    pub fn list_module_installs_with_containers(
        &self,
    ) -> Result<Vec<(String, String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, module_id, container_name
                   FROM module_installs
                  WHERE container_name IS NOT NULL AND container_name != ''",
            )
            .map_err(|e| format!("prepare list_containers: {}", e))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| format!("query list_containers: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_containers: {}", e))
    }

    /// v0.2.31 (#20-Fix-3): update the `module_version` + bump `installed_at`
    /// on an existing module_install row. Called by `update_module_for_project`
    /// after a successful in-place upgrade so the catalog UI reflects the new
    /// version without forcing the user through an uninstall+reinstall.
    ///
    /// Does NOT touch `status`, `enabled`, `last_started_at`, `last_error`,
    /// or `container_name` — the row is being updated in place, those fields
    /// stay as they were. Callers that want to flip status to Installed (e.g.
    /// the update path) must call `set_module_status` explicitly afterward.
    pub fn update_module_install_version(
        &self,
        project_id: &str,
        module_id: &str,
        new_version: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs
                    SET module_version = ?1,
                        installed_at = ?2
                  WHERE project_id = ?3 AND module_id = ?4",
                params![new_version, now, project_id, module_id],
            )
            .map_err(|e| format!("update module_install version: {}", e))?;
        if n == 0 {
            return Err(format!(
                "module_install not found for project={} module={}",
                project_id, module_id
            ));
        }
        Ok(())
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
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
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
                        container_name: row.get(10).ok().flatten(),
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
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
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
                    container_name: row.get(10).ok().flatten(),
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

    /// v0.2.33 (Agent C, reconciler support): list every module_install
    /// row whose `status` matches the requested string. Used by the
    /// launcher's startup reconciler to walk `installed` rows + verify
    /// the on-disk extracted manifest is present.
    ///
    /// Returns rows ordered by `installed_at DESC` to match
    /// `list_module_installs_for_project`'s ordering convention (recent
    /// installs first — keeps reconciler reports deterministic).
    pub fn list_module_installs_with_status(
        &self,
        status: &str,
    ) -> Result<Vec<ModuleInstallRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                   FROM module_installs
                  WHERE status = ?1
                  ORDER BY installed_at DESC",
            )
            .map_err(|e| format!("prepare list_module_installs_with_status: {}", e))?;
        let rows = stmt
            .query_map(params![status], |row| {
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
                    container_name: row.get(10).ok().flatten(),
                })
            })
            .map_err(|e| format!("query list_module_installs_with_status: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_module_installs_with_status: {}", e))
    }

    /// v0.2.33 (Agent C, reconciler support): flip the `status` column
    /// of a single module_install row identified by its primary key
    /// (the UUID assigned at `insert_module_install` time).
    ///
    /// Differs from `set_module_status` which keys by
    /// `(project_id, module_id)` and ALSO touches `last_error` +
    /// `last_started_at`. This setter is narrower: it only updates
    /// `status`. The reconciler doesn't need the side-effects of
    /// `set_module_status` and the by-id key is the natural shape for
    /// a "list, iterate, flip" pattern.
    ///
    /// Caller is responsible for ensuring `status` is a valid CHECK
    /// value (`'installing'`, `'installed'`, `'running'`, `'stopped'`,
    /// `'error'`, or `'broken'`). Invalid values cause the UPDATE to
    /// fail with a CHECK violation — surfaced as `Err`.
    pub fn set_module_install_status(
        &self,
        install_id: &str,
        status: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs SET status = ?1 WHERE id = ?2",
                params![status, install_id],
            )
            .map_err(|e| format!("set_module_install_status: {}", e))?;
        if n == 0 {
            return Err(format!(
                "module_install not found for id={}",
                install_id
            ));
        }
        Ok(())
    }
}
