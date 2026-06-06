//! Row-level CRUD for `module_installs`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::{ModuleInstallRow, ModuleStatus};
use super::Db;

/// v0.2.49 Stream A: shared row → `ModuleInstallRow` projector.
///
/// Centralizes the 11-column projection (id, project_id, module_id,
/// module_version, install_path, status, enabled, installed_at,
/// last_started_at, last_error, container_name) so every accessor that
/// reads `module_installs` produces a byte-identical `ModuleInstallRow`.
///
/// Post-027: `project_id` is nullable and projects as `Option<String>`.
/// Pre-027 callers (which expected `String`) won't compile against the
/// updated `ModuleInstallRow.project_id` field — the type change forces
/// every caller through the option-aware path, which is the
/// architectural guarantee Stream A needs.
fn row_to_install_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ModuleInstallRow> {
    let status_s: String = row.get(5)?;
    let enabled_i: i32 = row.get(6)?;
    Ok(ModuleInstallRow {
        id: row.get(0)?,
        project_id: row.get::<_, Option<String>>(1)?,
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
}

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
        // ON CONFLICT clause now targets the partial unique index
        // `idx_mi_unique_per_project` (migration 027). Pre-027 it
        // referenced the table-level `UNIQUE(project_id, module_id)`;
        // post-027 the table-level constraint is gone — superseded by
        // a partial unique index gated on `project_id IS NOT NULL`. The
        // ON CONFLICT clause references the same column tuple by name,
        // which SQLite resolves to the partial index. Every install-time
        // column is reset to the new value so a retried/upgraded install
        // row is indistinguishable from a fresh first install.
        guard
            .execute(
                "INSERT INTO module_installs
                 (id, project_id, module_id, module_version, install_path,
                  status, enabled, installed_at, last_started_at, last_error,
                  container_name)
                 VALUES (?1, ?2, ?3, ?4, ?5, 'installing', 1, ?6, NULL, NULL, NULL)
                 ON CONFLICT(project_id, module_id) WHERE project_id IS NOT NULL DO UPDATE SET
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
        // Re-read so the caller sees the actual row.
        let row = guard
            .query_row(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                   FROM module_installs
                  WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
                |row| row_to_install_row(row),
            )
            .map_err(|e| format!("read back module_install after upsert: {}", e))?;
        Ok(row)
    }

    /// v0.2.49 Stream A: insert (or upsert) a GLOBAL install row —
    /// `project_id IS NULL`, exactly one row per machine for this module.
    ///
    /// Mirrors `insert_module_install`'s upsert semantics (so retries,
    /// version upgrades, and reinstalls all flow through the same path)
    /// but targets the partial unique index `idx_mi_unique_global`
    /// (migration 027) instead of `idx_mi_unique_per_project`. The
    /// `WHERE project_id IS NULL` clause on the conflict target reflects
    /// the partial-index predicate — SQLite requires this match for the
    /// index to drive the conflict resolution.
    ///
    /// Returns the row as it lives in the DB after the upsert. The `id`
    /// column is preserved across conflicts (matches the per-project
    /// `insert_module_install` contract).
    pub fn insert_global_module_install(
        &self,
        id: &str,
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
                  status, enabled, installed_at, last_started_at, last_error,
                  container_name)
                 VALUES (?1, NULL, ?2, ?3, ?4, 'installing', 1, ?5, NULL, NULL, NULL)
                 ON CONFLICT(module_id) WHERE project_id IS NULL DO UPDATE SET
                     module_version  = excluded.module_version,
                     install_path    = excluded.install_path,
                     status          = excluded.status,
                     enabled         = excluded.enabled,
                     installed_at    = excluded.installed_at,
                     last_started_at = excluded.last_started_at,
                     last_error      = excluded.last_error,
                     container_name  = excluded.container_name",
                params![id, module_id, module_version, install_path, now],
            )
            .map_err(|e| format!("insert global module_install: {}", e))?;
        let row = guard
            .query_row(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                   FROM module_installs
                  WHERE project_id IS NULL AND module_id = ?1",
                params![module_id],
                |row| row_to_install_row(row),
            )
            .map_err(|e| format!("read back global module_install after upsert: {}", e))?;
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
    ///
    /// v0.2.49 Stream A: `project_id` is now `Option<String>` because
    /// global-scope install rows carry `project_id = NULL`. Callers that
    /// previously assumed `project_id: String` must now branch on `None`
    /// for global-scope rows. The container_name column is still
    /// `String` (non-null + non-empty per the WHERE clause).
    pub fn list_module_installs_with_containers(
        &self,
    ) -> Result<Vec<(Option<String>, String, String)>, String> {
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
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| format!("query list_containers: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_containers: {}", e))
    }

    /// v0.2.40 (NEW-3.E): list every `status='installed'` install row,
    /// REGARDLESS of whether `container_name` is set. Returns the same
    /// `(project_id, module_id, container_name)` triple shape as
    /// `list_module_installs_with_containers`, but `container_name` is
    /// `Option<String>` because rows whose install-time container start
    /// failed (or never ran — pre-NEW-3.B installs) have NULL here.
    ///
    /// Sibling of `list_module_installs_with_containers`. The two queries
    /// are intentionally distinct:
    ///
    ///   * `list_module_installs_with_containers` — narrow, used by
    ///     callers that only care about KNOWN running containers (e.g.
    ///     uninstall enumeration).
    ///   * `list_module_installs_needing_start` — broad, used by the
    ///     resume-on-boot sweep in
    ///     `vct-hub::module_supervisor::resume_containers_on_startup`,
    ///     which must consider BOTH:
    ///     (a) rows with a known container_name (existing path:
    ///     probe + restart),
    ///     (b) rows whose install-time auto-start failed BEFORE
    ///     NEW-3.B's default synthesis was available (newly
    ///     covered: synthesize defaults + start via
    ///     `start_container_after_install`).
    ///
    /// Filters `status='installed'` only (excludes `'error'`,
    ///` 'installing'`, `'broken'`). Caller is responsible for the
    /// runtime-type gate (`runtime.type ∈ {container, service}` AND
    /// `install.method = container_pull`) by loading each row's
    /// manifest — `list_module_installs_with_status` already follows
    /// that pattern, and the manifest must be loaded per-row anyway
    /// to call `start_container_after_install`.
    pub fn list_module_installs_needing_start(
        &self,
    ) -> Result<Vec<(Option<String>, String, Option<String>)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, module_id, container_name
                   FROM module_installs
                  WHERE status = 'installed'",
            )
            .map_err(|e| format!("prepare list_module_installs_needing_start: {}", e))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            })
            .map_err(|e| format!("query list_module_installs_needing_start: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_module_installs_needing_start: {}", e))
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

    /// NEW-3.C (2026-05-28): write an error message to `module_installs.last_error`
    /// WITHOUT touching `status`. Used when `start_container_after_install` fails
    /// post-install so the GUI tile can render a clear failure state even though
    /// `status` stays `'installed'` (the install itself succeeded; only the
    /// container start failed — the user can retry via Restart).
    ///
    /// Pass `None` to clear the field (e.g. on a subsequent successful start).
    pub fn set_module_last_error(
        &self,
        project_id: &str,
        module_id: &str,
        error: Option<&str>,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs SET last_error = ?1
                  WHERE project_id = ?2 AND module_id = ?3",
                params![error, project_id, module_id],
            )
            .map_err(|e| format!("set_module_last_error: {}", e))?;
        if n == 0 {
            return Err(format!(
                "module_install not found for project={} module={}",
                project_id, module_id
            ));
        }
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

    /// v0.2.45 V45-E: one-shot startup backfill of rows orphaned by the
    /// pre-v0.2.45 container-start-failure state-machine bug.
    ///
    /// Before v0.2.45, `start_container_after_install` failures called
    /// `set_module_last_error` WITHOUT a status flip. The resulting rows
    /// landed in:
    ///   status='installed' + last_error != NULL + container_name IS NULL
    /// — invisible to V44-G4's `status IN ('error', 'broken')` auto-retry
    /// predicate (see `module_service::retry_failed_module_installs`),
    /// stranding paid-module rows in a half-state that only manual
    /// "Reinstall" clicks could recover from.
    ///
    /// This UPDATE flips those rows to `status='error'` so V44-G4 picks
    /// them up on the next orchestrator-update sweep. Three safety
    /// properties:
    ///
    ///   * **Idempotent** — the WHERE clause excludes rows already at
    ///     `status='error'`, so a second run is a no-op (returns 0).
    ///   * **Conservative** — only touches rows that match ALL of:
    ///     status='installed' + last_error IS NOT NULL + container_name
    ///     IS NULL. Legitimately-installed rows (status='installed' with
    ///     a container_name set) are untouched. Rows that already have
    ///     status='error' are untouched.
    ///   * **Non-destructive** — UPDATE only, no DELETE. The
    ///     `last_error` payload is preserved for the GUI tile and for
    ///     V44-G4's audit log.
    ///
    /// Returns the row count for the audit log. Soft-fail callers should
    /// `eprintln!` and continue on Err — backfill failure must NEVER
    /// block launcher boot (the existing rows just stay invisible to
    /// auto-retry until the user clicks Reinstall manually).
    pub fn backfill_partial_container_start_failures(&self) -> Result<usize, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs
                    SET status = 'error'
                  WHERE status = 'installed'
                    AND last_error IS NOT NULL
                    AND container_name IS NULL",
                [],
            )
            .map_err(|e| {
                format!(
                    "backfill_partial_container_start_failures: {}",
                    e
                )
            })?;
        Ok(n)
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
                |row| row_to_install_row(row),
            )
            .optional()
            .map_err(|e| format!("get module_install: {}", e))
    }

    /// v0.2.49 Stream A: read the GLOBAL install row for a module
    /// (`project_id IS NULL`). Returns `None` when no global row exists
    /// — caller should fall back to per-project rows in that case.
    pub fn get_global_module_install(
        &self,
        module_id: &str,
    ) -> Result<Option<ModuleInstallRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                 FROM module_installs
                 WHERE project_id IS NULL AND module_id = ?1",
                params![module_id],
                |row| row_to_install_row(row),
            )
            .optional()
            .map_err(|e| format!("get global module_install: {}", e))
    }

    /// v0.2.49 Stream A: delete the GLOBAL install row for a module.
    /// Returns `Ok(())` whether or not the row existed (matches
    /// `delete_module_install`'s contract).
    pub fn delete_global_module_install(&self, module_id: &str) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_installs WHERE project_id IS NULL AND module_id = ?1",
                params![module_id],
            )
            .map_err(|e| format!("delete global module_install: {}", e))?;
        Ok(())
    }

    /// v0.2.49 Stream A: write `last_error` on a GLOBAL install row
    /// without touching `status`. Sibling of
    /// [`Db::set_module_last_error`] for per-project rows. Used by the
    /// resume sweep when a global container's auto-start fails.
    pub fn set_global_module_last_error(
        &self,
        module_id: &str,
        error: Option<&str>,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs SET last_error = ?1
                  WHERE project_id IS NULL AND module_id = ?2",
                params![error, module_id],
            )
            .map_err(|e| format!("set_global_module_last_error: {}", e))?;
        if n == 0 {
            return Err(format!(
                "global module_install not found for module={}",
                module_id
            ));
        }
        Ok(())
    }

    /// v0.2.49 Stream A: flip status on a GLOBAL install row. Sibling
    /// of [`Db::set_module_status`] for per-project rows.
    pub fn set_global_module_status(
        &self,
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
                  WHERE project_id IS NULL AND module_id = ?4",
                params![status.as_str(), error, started_at, module_id],
            )
            .map_err(|e| format!("set_global_module_status: {}", e))?;
        Ok(())
    }

    /// v0.2.49 Stream A: persist the resolved container name on a GLOBAL
    /// install row. Sibling of [`Db::set_module_container_name`] for
    /// per-project rows.
    pub fn set_global_module_container_name(
        &self,
        module_id: &str,
        container_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE module_installs SET container_name = ?1
                  WHERE project_id IS NULL AND module_id = ?2",
                params![container_name, module_id],
            )
            .map_err(|e| format!("set global container_name: {}", e))?;
        if n == 0 {
            return Err(format!(
                "global module_install not found for module={}",
                module_id
            ));
        }
        Ok(())
    }

    /// v0.2.49 Stream A: list every per-project row for a given module
    /// id. Used by the auto-migration path that converts a module from
    /// per-project to global scope (delete N per-project rows + spawn 1
    /// global row).
    pub fn list_per_project_installs_for_module(
        &self,
        module_id: &str,
    ) -> Result<Vec<ModuleInstallRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                   FROM module_installs
                  WHERE project_id IS NOT NULL AND module_id = ?1",
            )
            .map_err(|e| format!("prepare list_per_project_installs_for_module: {}", e))?;
        let rows = stmt
            .query_map(params![module_id], |row| row_to_install_row(row))
            .map_err(|e| format!("query list_per_project_installs_for_module: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_per_project_installs_for_module: {}", e))
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
            .query_map(params![project_id], |row| row_to_install_row(row))
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// v0.2.49 Stream A: list every GLOBAL install row (project_id IS NULL).
    pub fn list_global_module_installs(&self) -> Result<Vec<ModuleInstallRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, module_id, module_version, install_path,
                        status, enabled, installed_at, last_started_at, last_error,
                        container_name
                   FROM module_installs
                  WHERE project_id IS NULL
                  ORDER BY installed_at DESC",
            )
            .map_err(|e| format!("prepare list_global_module_installs: {}", e))?;
        let rows = stmt
            .query_map([], |row| row_to_install_row(row))
            .map_err(|e| format!("query list_global_module_installs: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_global_module_installs: {}", e))
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
            .query_map(params![status], |row| row_to_install_row(row))
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

    // ─── NEW-3.C (2026-05-28): set_module_last_error tests ───────────────────
    //
    // These tests verify that `set_module_last_error` writes and clears
    // `last_error` without touching `status`, mirroring the install-time
    // container-start failure path in `modules.rs:1450–1471`.

    /// Verify that `set_module_last_error` persists an error string to
    /// `module_installs.last_error` while leaving `status` unchanged.
    #[test]
    fn db_set_module_last_error_persists_error() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-id-lasterror-1",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/home/test/.vct/modules/vct-rl-reranker",
        )
        .expect("insert");
        // Flip to Installed (mimics the successful install step before
        // the container start is attempted).
        db.set_module_status(&pid, "vct-rl-reranker", ModuleStatus::Installed, None)
            .expect("set installed");

        // Simulate container-start failure.
        db.set_module_last_error(
            &pid,
            "vct-rl-reranker",
            Some("podman: image not found"),
        )
        .expect("set_module_last_error must succeed");

        let row = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        // Error is visible.
        assert_eq!(
            row.last_error.as_deref(),
            Some("podman: image not found"),
            "last_error must be written by set_module_last_error",
        );
        // Status must NOT have changed — install succeeded, only start failed.
        assert_eq!(
            row.status,
            ModuleStatus::Installed,
            "status must remain Installed after set_module_last_error",
        );
    }

    /// Verify that passing `None` to `set_module_last_error` clears the
    /// field — models the "user retries, container starts successfully"
    /// flow where `last_error` should be wiped.
    #[test]
    fn db_set_module_last_error_to_none_clears_field() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-id-lasterror-2",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/home/test/.vct/modules/vct-rl-reranker",
        )
        .expect("insert");
        db.set_module_status(&pid, "vct-rl-reranker", ModuleStatus::Installed, None)
            .expect("set installed");

        // Write then clear.
        db.set_module_last_error(
            &pid,
            "vct-rl-reranker",
            Some("temporary failure"),
        )
        .expect("write error");
        db.set_module_last_error(&pid, "vct-rl-reranker", None)
            .expect("clear error");

        let row = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert!(
            row.last_error.is_none(),
            "last_error must be NULL after set_module_last_error(None)",
        );
        // Status must remain Installed throughout.
        assert_eq!(row.status, ModuleStatus::Installed);
    }

    /// v0.2.40 (NEW-3.E): `list_module_installs_needing_start` must
    /// return BOTH `status='installed'` rows with non-NULL
    /// `container_name` AND rows with NULL `container_name`. This is the
    /// behaviour difference vs `list_module_installs_with_containers`
    /// (which filters to non-NULL only) — the resume-on-boot sweep
    /// needs to consider NULL-container rows because that's the
    /// failure-mode this branch fixes (install-time start failed →
    /// container_name was never persisted).
    #[test]
    fn db_list_module_installs_needing_start_returns_null_container_rows() {
        let (db, pid) = open_db_with_project();

        // Row A: status=installed, container_name=NULL (the v0.2.40
        // failure-mode case — install-time start hadn't run or failed
        // before NEW-3.B synthesis).
        db.insert_module_install(
            "install-a-null",
            &pid,
            "module-null",
            "0.1.0",
            "/tmp/install-a",
        )
        .expect("insert A");
        db.set_module_status(&pid, "module-null", ModuleStatus::Installed, None)
            .expect("set A installed");

        // Row B: status=installed, container_name=SET (the existing
        // path — already-resolved container, supervisor will probe + restart).
        db.insert_module_install(
            "install-b-named",
            &pid,
            "module-named",
            "0.1.0",
            "/tmp/install-b",
        )
        .expect("insert B");
        db.set_module_status(&pid, "module-named", ModuleStatus::Installed, None)
            .expect("set B installed");
        db.set_module_container_name(&pid, "module-named", "named-container")
            .expect("set container_name");

        // Row C: status=error — must be EXCLUDED (only `installed` rows
        // are resumable; `error` rows require user action).
        db.insert_module_install(
            "install-c-err",
            &pid,
            "module-err",
            "0.1.0",
            "/tmp/install-c",
        )
        .expect("insert C");
        db.set_module_status(
            &pid,
            "module-err",
            ModuleStatus::Error,
            Some("oops".into()),
        )
        .expect("set C error");

        // Row D: status=installing — must be EXCLUDED (still in
        // install pipeline; resume sweep mustn't interfere).
        db.insert_module_install(
            "install-d-inst",
            &pid,
            "module-installing",
            "0.1.0",
            "/tmp/install-d",
        )
        .expect("insert D");
        // Status stays 'installing' (set by insert_module_install).

        let rows = db
            .list_module_installs_needing_start()
            .expect("query must succeed");

        // A and B in, C and D out.
        let module_ids: Vec<&str> = rows.iter().map(|(_, m, _)| m.as_str()).collect();
        assert!(
            module_ids.contains(&"module-null"),
            "NULL-container installed row must be returned (the v0.2.40 bug-fix case): rows={:?}",
            rows
        );
        assert!(
            module_ids.contains(&"module-named"),
            "SET-container installed row must also be returned (existing path): rows={:?}",
            rows
        );
        assert!(
            !module_ids.contains(&"module-err"),
            "error-status row must be EXCLUDED: rows={:?}",
            rows
        );
        assert!(
            !module_ids.contains(&"module-installing"),
            "installing-status row must be EXCLUDED: rows={:?}",
            rows
        );

        // Verify the NULL/Some shape of the container_name column.
        for (_, module_id, container_name) in &rows {
            if module_id == "module-null" {
                assert!(
                    container_name.is_none(),
                    "module-null row must surface container_name=None, got {:?}",
                    container_name
                );
            } else if module_id == "module-named" {
                assert_eq!(
                    container_name.as_deref(),
                    Some("named-container"),
                    "module-named row must surface its persisted container_name"
                );
            }
        }
    }

    // ─── v0.2.45 V45-E: backfill_partial_container_start_failures ───────
    //
    // These tests cover the one-shot startup backfill that flips
    // pre-v0.2.45 stuck rows (status='installed' + last_error != NULL +
    // container_name IS NULL) to status='error' so V44-G4 auto-retry
    // can heal them.

    /// Helper: seed a row in the exact partial-failure shape we're
    /// migrating away from. Mirrors what a pre-v0.2.45 launcher would
    /// have written when `start_container_after_install` failed
    /// post-pull: status stays 'installed', last_error is set,
    /// container_name is NULL.
    fn seed_partial_failure_row(
        db: &Db,
        project_id: &str,
        module_id: &str,
        err_msg: &str,
    ) {
        db.insert_module_install(
            &format!("install-id-{}", module_id),
            project_id,
            module_id,
            "0.2.7",
            "/tmp/install-path",
        )
        .expect("seed insert");
        // Status starts as 'installing' from insert; flip to
        // 'installed' to match what a successful install + failed
        // container-start would have left behind pre-v0.2.45.
        db.set_module_status(project_id, module_id, ModuleStatus::Installed, None)
            .expect("seed: flip to installed");
        db.set_module_last_error(project_id, module_id, Some(err_msg))
            .expect("seed: set last_error");
        // container_name intentionally NOT set — that's the stuck
        // shape (post-pull, pre-container-start).
    }

    /// V45-E backfill flips a stuck row from 'installed' → 'error'.
    /// The row was in the exact shape produced by a pre-v0.2.45
    /// container-start-failure: status='installed', last_error != NULL,
    /// container_name IS NULL. Post-backfill it must be visible to
    /// V44-G4's `status IN ('error', 'broken')` predicate.
    #[test]
    fn v0245_backfill_flips_stuck_row_to_error() {
        let (db, pid) = open_db_with_project();
        seed_partial_failure_row(
            &db,
            &pid,
            "vct-rl-reranker",
            "podman start exited 125: image not found",
        );

        let pre = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert_eq!(pre.status, ModuleStatus::Installed);
        assert!(pre.last_error.is_some());
        assert!(pre.container_name.is_none());

        let n = db
            .backfill_partial_container_start_failures()
            .expect("backfill must succeed");
        assert_eq!(n, 1, "exactly one stuck row should be flipped");

        let post = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert_eq!(
            post.status,
            ModuleStatus::Error,
            "stuck row must be flipped to 'error' for V44-G4 visibility"
        );
        // last_error preserved — the GUI tile and audit log still
        // need it.
        assert_eq!(
            post.last_error.as_deref(),
            Some("podman start exited 125: image not found"),
            "last_error payload must be preserved on backfill"
        );
        assert!(
            post.container_name.is_none(),
            "container_name stays NULL (we don't synthesize one)"
        );
    }

    /// V45-E backfill is idempotent: running it twice on the same DB
    /// returns 0 on the second call. Required because the backfill
    /// runs on EVERY launcher startup — re-running on already-healed
    /// rows must be a clean no-op.
    #[test]
    fn v0245_backfill_is_idempotent() {
        let (db, pid) = open_db_with_project();
        seed_partial_failure_row(&db, &pid, "vct-rl-reranker", "failure 1");

        let n1 = db
            .backfill_partial_container_start_failures()
            .expect("first backfill");
        assert_eq!(n1, 1, "first run flips the stuck row");

        let n2 = db
            .backfill_partial_container_start_failures()
            .expect("second backfill");
        assert_eq!(
            n2, 0,
            "second run must be a no-op (row is already at 'error')"
        );
    }

    /// V45-E backfill must NOT touch rows that are legitimately
    /// 'installed' (i.e. have a container_name set and no last_error).
    /// Those are healthy installed modules — leaving them as 'installed'
    /// is correct.
    #[test]
    fn v0245_backfill_does_not_touch_healthy_installed_rows() {
        let (db, pid) = open_db_with_project();

        // Row 1: stuck partial failure → must be flipped.
        seed_partial_failure_row(&db, &pid, "stuck-mod", "container start failed");
        // Row 2: healthy install with container_name set, no last_error
        // → must be left alone.
        db.insert_module_install(
            "install-id-healthy",
            &pid,
            "healthy-mod",
            "0.2.7",
            "/tmp/healthy",
        )
        .expect("seed healthy");
        db.set_module_status(&pid, "healthy-mod", ModuleStatus::Installed, None)
            .expect("flip healthy to installed");
        db.set_module_container_name(&pid, "healthy-mod", "healthy-container")
            .expect("set container_name on healthy");

        let n = db
            .backfill_partial_container_start_failures()
            .expect("backfill");
        assert_eq!(n, 1, "only the stuck row should be flipped");

        let stuck = db
            .get_module_install(&pid, "stuck-mod")
            .unwrap()
            .unwrap();
        assert_eq!(stuck.status, ModuleStatus::Error);

        let healthy = db
            .get_module_install(&pid, "healthy-mod")
            .unwrap()
            .unwrap();
        assert_eq!(
            healthy.status,
            ModuleStatus::Installed,
            "healthy row must be untouched (container_name was set)"
        );
        assert_eq!(
            healthy.container_name.as_deref(),
            Some("healthy-container")
        );
    }

    /// V45-E backfill must NOT touch rows that are already at
    /// 'error'. Those have either been flipped by V45-E's modules.rs
    /// patch (the steady-state path post-v0.2.45) or by some other
    /// path. The WHERE clause filters on status='installed' precisely
    /// to leave them alone.
    #[test]
    fn v0245_backfill_does_not_touch_already_error_rows() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-id-1",
            &pid,
            "already-errored-mod",
            "0.2.7",
            "/tmp/x",
        )
        .expect("seed");
        // This row mirrors what a POST-v0.2.45 container-start-failure
        // produces: status='error' + last_error + container_name=NULL.
        db.set_module_status(
            &pid,
            "already-errored-mod",
            ModuleStatus::Error,
            Some("already in error state".into()),
        )
        .expect("seed to error");

        let n = db
            .backfill_partial_container_start_failures()
            .expect("backfill");
        assert_eq!(
            n, 0,
            "row already at 'error' must be excluded by status='installed' filter"
        );

        let row = db
            .get_module_install(&pid, "already-errored-mod")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, ModuleStatus::Error);
        assert_eq!(
            row.last_error.as_deref(),
            Some("already in error state"),
            "last_error must be preserved (no double-touch)"
        );
    }

    // ─── v0.2.49 Stream A: global install row tests ─────────────────────

    /// Inserting a global row produces `project_id = None`.
    #[test]
    fn v0249_global_install_row_has_null_project_id() {
        let db = Db::open_in_memory().expect("in-memory db");
        let row = db
            .insert_global_module_install(
                "install-global-1",
                "vct-rl-reranker",
                "0.2.10",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("global insert must succeed");
        assert_eq!(row.id, "install-global-1");
        assert!(row.project_id.is_none(), "global row must have project_id=None");
        assert_eq!(row.module_id, "vct-rl-reranker");
        assert_eq!(row.status, ModuleStatus::Installing);
    }

    /// `insert_global_module_install` is an upsert: second call returns
    /// the same id, fresh status, version refreshed.
    #[test]
    fn v0249_global_install_row_upserts_on_retry() {
        let db = Db::open_in_memory().expect("in-memory db");
        let first = db
            .insert_global_module_install(
                "install-global-1",
                "vct-rl-reranker",
                "0.2.9",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("first global insert");
        db.set_global_module_status(
            "vct-rl-reranker",
            ModuleStatus::Error,
            Some("pull failed".into()),
        )
        .expect("flip to error");

        // Retry with new version — must succeed via upsert, preserve id.
        let retried = db
            .insert_global_module_install(
                "install-global-2-IGNORED",
                "vct-rl-reranker",
                "0.2.10",
                "/home/test/.vct/modules/vct-rl-reranker",
            )
            .expect("retry upsert");
        assert_eq!(retried.id, first.id, "id must be preserved across upsert");
        assert_eq!(retried.module_version, "0.2.10", "version refreshed");
        assert_eq!(retried.status, ModuleStatus::Installing);
        assert!(retried.last_error.is_none(), "last_error cleared");
    }

    /// At most ONE global row per module_id — second distinct insert is
    /// not allowed (the partial unique index on `WHERE project_id IS NULL`
    /// enforces this).
    #[test]
    fn v0249_global_install_row_unique_per_module() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_global_module_install(
            "install-global-1",
            "vct-rl-reranker",
            "0.2.10",
            "/p1",
        )
        .expect("first global insert");
        // Second insert with same module_id → upsert (not a new row).
        // Distinct module_id → independent row.
        let r2 = db
            .insert_global_module_install(
                "install-global-2",
                "vct-rl-reranker",
                "0.2.11",
                "/p2",
            )
            .expect("same module_id upserts");
        // Only one row total for this module_id.
        let rows = db.list_global_module_installs().expect("list");
        assert_eq!(rows.len(), 1, "exactly one global row for one module_id");
        assert_eq!(r2.module_version, "0.2.11");
    }

    /// Global row + per-project row for the SAME module coexist (the
    /// auto-migration creates this state transiently before deleting the
    /// per-project rows).
    #[test]
    fn v0249_global_and_per_project_rows_coexist() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-pp-1",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/per-project",
        )
        .expect("per-project insert");
        db.insert_global_module_install(
            "install-global-1",
            "vct-rl-reranker",
            "0.2.10",
            "/global",
        )
        .expect("global insert");

        // Both must be visible via their respective lookups.
        let pp = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert_eq!(pp.project_id.as_deref(), Some(pid.as_str()));
        assert_eq!(pp.module_version, "0.2.7");

        let g = db
            .get_global_module_install("vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert!(g.project_id.is_none());
        assert_eq!(g.module_version, "0.2.10");

        // list_per_project_installs_for_module returns ONLY the
        // per-project row (not the global one).
        let per_proj_rows = db
            .list_per_project_installs_for_module("vct-rl-reranker")
            .expect("list per-project");
        assert_eq!(per_proj_rows.len(), 1);
        assert!(per_proj_rows[0].project_id.is_some());
    }

    /// `delete_global_module_install` removes ONLY the global row.
    #[test]
    fn v0249_delete_global_module_install_leaves_per_project_rows() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install(
            "install-pp-1",
            &pid,
            "vct-rl-reranker",
            "0.2.7",
            "/per-project",
        )
        .expect("per-project insert");
        db.insert_global_module_install(
            "install-global-1",
            "vct-rl-reranker",
            "0.2.10",
            "/global",
        )
        .expect("global insert");

        db.delete_global_module_install("vct-rl-reranker")
            .expect("delete global");

        assert!(
            db.get_global_module_install("vct-rl-reranker")
                .unwrap()
                .is_none(),
            "global row gone"
        );
        assert!(
            db.get_module_install(&pid, "vct-rl-reranker")
                .unwrap()
                .is_some(),
            "per-project row preserved"
        );
    }

    /// `list_module_installs_needing_start` returns BOTH global and
    /// per-project rows (the supervisor's resume sweep needs both).
    #[test]
    fn v0249_list_needing_start_returns_global_and_per_project() {
        let (db, pid) = open_db_with_project();
        // Per-project installed row.
        db.insert_module_install("install-pp-1", &pid, "mod-pp", "0.1.0", "/pp")
            .expect("pp insert");
        db.set_module_status(&pid, "mod-pp", ModuleStatus::Installed, None)
            .expect("flip pp installed");
        // Global installed row.
        db.insert_global_module_install("install-g-1", "mod-g", "0.1.0", "/g")
            .expect("g insert");
        db.set_global_module_status("mod-g", ModuleStatus::Installed, None)
            .expect("flip g installed");

        let rows = db.list_module_installs_needing_start().expect("list");
        let module_ids: Vec<&str> = rows.iter().map(|(_, m, _)| m.as_str()).collect();
        assert!(module_ids.contains(&"mod-pp"), "per-project row included");
        assert!(module_ids.contains(&"mod-g"), "global row included");
        for (pid_opt, mod_id, _) in rows {
            if mod_id == "mod-g" {
                assert!(pid_opt.is_none(), "global row projects project_id=None");
            } else if mod_id == "mod-pp" {
                assert!(pid_opt.is_some(), "per-project row projects project_id=Some");
            }
        }
    }

    /// `set_global_module_container_name` writes only to the global row.
    #[test]
    fn v0249_set_global_container_name_writes_only_global_row() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install("install-pp", &pid, "vct-rl-reranker", "0.2.7", "/pp")
            .expect("pp insert");
        db.set_module_container_name(&pid, "vct-rl-reranker", "per-proj-container")
            .expect("set pp container");
        db.insert_global_module_install("install-g", "vct-rl-reranker", "0.2.10", "/g")
            .expect("g insert");

        db.set_global_module_container_name("vct-rl-reranker", "global-container")
            .expect("set global container");

        let pp_row = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        let g_row = db
            .get_global_module_install("vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert_eq!(pp_row.container_name.as_deref(), Some("per-proj-container"));
        assert_eq!(g_row.container_name.as_deref(), Some("global-container"));
    }

    /// `set_global_module_last_error` writes only to the global row.
    #[test]
    fn v0249_set_global_last_error_writes_only_global_row() {
        let (db, pid) = open_db_with_project();
        db.insert_module_install("install-pp", &pid, "vct-rl-reranker", "0.2.7", "/pp")
            .expect("pp insert");
        db.insert_global_module_install("install-g", "vct-rl-reranker", "0.2.10", "/g")
            .expect("g insert");

        db.set_global_module_last_error(
            "vct-rl-reranker",
            Some("global pull failed"),
        )
        .expect("set global last_error");

        let pp_row = db
            .get_module_install(&pid, "vct-rl-reranker")
            .unwrap()
            .unwrap();
        let g_row = db
            .get_global_module_install("vct-rl-reranker")
            .unwrap()
            .unwrap();
        assert!(pp_row.last_error.is_none(), "per-project last_error untouched");
        assert_eq!(g_row.last_error.as_deref(), Some("global pull failed"));
    }

    /// `list_per_project_installs_for_module` filters out global rows.
    #[test]
    fn v0249_list_per_project_excludes_global() {
        let (db, pid_a) = open_db_with_project();
        let pid_b = "test-proj-b".to_string();
        db.insert_project(
            &pid_b,
            "B",
            "/tmp/b",
            crate::db::models::ProjectHost::Base,
            "test-proj-b",
        )
        .expect("seed B");

        db.insert_module_install("install-pp-a", &pid_a, "vct-rl-reranker", "0.2.7", "/a")
            .expect("pp a");
        db.insert_module_install("install-pp-b", &pid_b, "vct-rl-reranker", "0.2.7", "/b")
            .expect("pp b");
        db.insert_global_module_install("install-g", "vct-rl-reranker", "0.2.10", "/g")
            .expect("global");

        let pp_rows = db
            .list_per_project_installs_for_module("vct-rl-reranker")
            .expect("list pp");
        assert_eq!(pp_rows.len(), 2, "two per-project rows, global excluded");
        for r in &pp_rows {
            assert!(r.project_id.is_some());
        }
    }
}
