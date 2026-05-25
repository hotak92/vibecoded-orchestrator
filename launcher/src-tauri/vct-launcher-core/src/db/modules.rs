//! Row-level CRUD for `module_installs`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::{ModuleInstallRow, ModuleStatus};
use super::Db;

impl Db {
    /// Insert (or upsert) a pending `module_installs` row for the
    /// `(project_id, module_id)` pair.
    ///
    /// v0.2.34: switched from raw INSERT to `INSERT … ON CONFLICT
    /// (project_id, module_id) DO UPDATE …`. The pre-v0.2.34 INSERT
    /// crashed with `UNIQUE constraint failed: module_installs
    /// .project_id, module_installs.module_id` in three real-world
    /// flows:
    ///
    ///   1. **Retry after error** — install fails, leaves a row at
    ///      status=`'error'`; the user clicks Install again and the
    ///      second insert collides. Pre-fix, the user had to manually
    ///      delete the row (no GUI surface). Post-fix, the upsert
    ///      resets status to `'installing'`, clears `last_error`, and
    ///      proceeds.
    ///   2. **Version upgrade via install path** — `update_module_for
    ///      _project` is the canonical update entry point and uses
    ///      `update_module_install_version`, but a same-id-different-
    ///      version install (e.g. catalog re-points to a newer
    ///      version after the user uninstalled+removed the manifest
    ///      hash and clicked Install) hit the same UNIQUE wall.
    ///   3. **Reinstall after partial uninstall** — uninstall ran the
    ///      pre-uninstall command but the row deletion failed (e.g.
    ///      podman down hung); the orphan row blocks reinstall until
    ///      the user finds the launcher DB and DELETEs by hand.
    ///
    /// The UPSERT resets every install-time column to the new values
    /// (so the row IS the new install, not a stale aggregate of two)
    /// while preserving the primary-key `id` of the existing row when
    /// one already exists — `id` is excluded from the DO UPDATE SET
    /// list, so FK references to it (none today, but defence in
    /// depth) stay valid. `container_name` is reset to NULL because
    /// the new install will re-resolve it via
    /// `set_module_container_name`.
    ///
    /// Returns the row as it now lives in the DB (re-read after the
    /// upsert so the caller sees the actual id, which may differ from
    /// the requested `id` when a prior row was preserved).
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
        // ON CONFLICT clause references the UNIQUE constraint on
        // (project_id, module_id) declared in 001_initial.sql. Every
        // install-time column is reset to the new value so a retried/
        // upgraded install row is indistinguishable from a fresh
        // first install.
        guard
            .execute(
                "INSERT INTO module_installs
                 (id, project_id, module_id, module_version, install_path,
                  status, enabled, installed_at, last_started_at, last_error,
                  container_name)
                 VALUES (?1, ?2, ?3, ?4, ?5, 'installing', 1, ?6, NULL, NULL, NULL)
                 ON CONFLICT(project_id, module_id) DO UPDATE SET
                     module_version  = excluded.module_version,
                     install_path    = excluded.install_path,
                     status          = excluded.status,
                     enabled         = excluded.enabled,
                     installed_at    = excluded.installed_at,
                     last_started_at = excluded.last_started_at,
                     last_error      = excluded.last_error,
                     container_name  = excluded.container_name",
                params![id, project_id, module_id, module_version, install_path, now],
            )
            .map_err(|e| format!("insert module_install: {}", e))?;
        // Re-read so the caller sees the actual row (the `id` column
        // is preserved on conflict; the requested `id` is ignored
        // when a row already exists). All other columns now match
        // the INSERT-side values per the DO UPDATE SET above.
        let row = guard
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
                        status: ModuleStatus::from_str(&status_s)
                            .unwrap_or(ModuleStatus::Error),
                        enabled: enabled_i != 0,
                        installed_at: row.get(7)?,
                        last_started_at: row.get(8)?,
                        last_error: row.get(9)?,
                        container_name: row.get(10).ok().flatten(),
                    })
                },
            )
            .map_err(|e| format!("read back module_install after upsert: {}", e))?;
        Ok(row)
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

// ─── v0.2.34: insert_module_install UPSERT regression tests ────────
//
// Four scenarios pin the post-v0.2.34 idempotency guarantee:
//
//   1. fresh insert                — no prior row, normal first-install
//   2. retry after error           — prior row at status='error'
//   3. version upgrade             — prior row with older module_version
//   4. reinstall after partial uninstall — prior row at status='installed'
//
// Pre-v0.2.34 the second-through-fourth cases crashed with
// `UNIQUE constraint failed: module_installs.project_id,
// module_installs.module_id`. Post-v0.2.34 they all succeed and
// reset the row's install-time columns to a fresh installing state.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::{ModuleStatus, ProjectHost};

    /// Build an in-memory Db with one project row so the FK on
    /// `module_installs.project_id` resolves. Returns `(db, project_id)`.
    fn open_db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "test-proj-upsert".to_string();
        db.insert_project(
            &id,
            "Test Project",
            "/tmp/test-proj-upsert",
            ProjectHost::Base,
            "test-project-upsert",
        )
        .expect("insert project");
        (db, id)
    }

    /// Case 1: fresh insert — no prior row. The row lands with the
    /// requested id, status='installing', no last_error.
    #[test]
    fn insert_module_install_fresh_insert() {
        let (db, pid) = open_db_with_project();
        let row = db
            .insert_module_install(
                "install-id-1",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("fresh insert must succeed");
        assert_eq!(row.id, "install-id-1");
        assert_eq!(row.module_version, "0.2.7");
        assert_eq!(row.status, ModuleStatus::Installing);
        assert!(row.last_error.is_none());
        assert!(row.container_name.is_none());
        assert!(row.enabled);
    }

    /// Case 2: retry after error. A prior row at status='error' must
    /// upsert into a fresh installing state. Pre-v0.2.34 this hit
    /// UNIQUE constraint failed.
    #[test]
    fn insert_module_install_upserts_on_retry_after_error() {
        let (db, pid) = open_db_with_project();

        // Seed: one prior row, flipped to error with a message.
        let first = db
            .insert_module_install(
                "install-id-1",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("first insert");
        db.set_module_status(
            &pid,
            "vct-rl-reranker",
            ModuleStatus::Error,
            Some("podman pull failed: 500".into()),
        )
        .expect("flip to error");
        let errored = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert_eq!(errored.status, ModuleStatus::Error);
        assert_eq!(
            errored.last_error.as_deref(),
            Some("podman pull failed: 500"),
        );

        // Retry: same (project, module). Pre-v0.2.34 this returned
        // `Err(UNIQUE constraint failed: …)` — now succeeds and
        // resets `status`+`last_error`.
        let retried = db
            .insert_module_install(
                "install-id-2-IGNORED-ON-CONFLICT",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("retry must succeed (v0.2.34 upsert)");
        // `id` is preserved across conflict (kept the existing row's
        // primary key).
        assert_eq!(retried.id, first.id, "id must be preserved on conflict");
        assert_eq!(retried.status, ModuleStatus::Installing);
        assert!(
            retried.last_error.is_none(),
            "last_error must be cleared on upsert; got {:?}",
            retried.last_error
        );
    }

    /// Case 3: version upgrade — same (project, module), new version.
    /// Architectural goal: v0.2.7 → v0.2.8 install via the install
    /// path (not just `update_module_install_version`) must work.
    #[test]
    fn insert_module_install_upserts_on_version_upgrade() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-id-1",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/home/test/.vct/modules/vct-rl-reranker",
        )
        .expect("v0.2.7 install");

        // New version, same project+module pair.
        let upgraded = db
            .insert_module_install(
                "install-id-2-IGNORED",
                &pid,
                "vct-rl-reranker",
                "0.2.8",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("v0.2.8 install via upsert must succeed");
        assert_eq!(upgraded.module_version, "0.2.8");
        assert_eq!(upgraded.status, ModuleStatus::Installing);
    }

    /// Case 4: reinstall after partial uninstall — prior row still
    /// at status='installed' (uninstall left orphan row behind).
    /// Pre-v0.2.34 the user had to manually delete the row.
    #[test]
    fn insert_module_install_upserts_on_reinstall_after_uninstall() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-id-1",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/home/test/.vct/modules/vct-rl-reranker",
        )
        .expect("first install");
        db.set_module_status(
            &pid,
            "vct-rl-reranker",
            ModuleStatus::Installed,
            None,
        )
        .expect("flip to installed");

        // Click "Install" again without ever deleting the row.
        let reinstalled = db
            .insert_module_install(
                "install-id-2-IGNORED",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("reinstall must succeed (v0.2.34 upsert)");
        assert_eq!(reinstalled.status, ModuleStatus::Installing);
        assert!(reinstalled.last_error.is_none());
    }

    /// Sanity: distinct (project, module) pairs still produce
    /// independent rows. The upsert must NOT collapse different
    /// modules under the same project (or vice versa).
    #[test]
    fn insert_module_install_distinct_pairs_stay_independent() {
        let (db, pid_a) = open_db_with_project();
        let pid_b = "test-proj-upsert-2".to_string();
        db.insert_project(
            &pid_b,
            "Project B",
            "/tmp/test-proj-upsert-2",
            ProjectHost::Base,
            "test-project-upsert-2",
        )
        .expect("insert project B");

        db.insert_module_install(
            "install-a", &pid_a, "vct-rl-reranker", "0.2.7", "/path/a",
        ).unwrap();
        db.insert_module_install(
            "install-b", &pid_b, "vct-rl-reranker", "0.2.7", "/path/b",
        ).unwrap();
        db.insert_module_install(
            "install-c", &pid_a, "vct-coordination", "0.1.0", "/path/c",
        ).unwrap();

        assert_eq!(
            db.list_module_installs_for_project(&pid_a).unwrap().len(),
            2,
            "project A holds 2 distinct modules",
        );
        assert_eq!(
            db.list_module_installs_for_project(&pid_b).unwrap().len(),
            1,
            "project B holds 1 module",
        );
    }
}
