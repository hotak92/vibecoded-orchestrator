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
            rl_port: None,
        })
    }

    pub fn get_project(&self, id: &str) -> Result<Option<ProjectRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, name, folder_path, host, slug, created_at, updated_at, rl_port
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
                "SELECT id, name, folder_path, host, slug, created_at, updated_at, rl_port
                 FROM projects WHERE slug = ?1",
                params![slug],
                row_to_project,
            )
            .optional()
            .map_err(|e| format!("get project by slug: {}", e))
    }

    pub fn list_projects(&self) -> Result<Vec<ProjectRow>, String> {
        let guard = self.lock();
        Self::list_projects_with_guard(&guard)
    }

    /// v0.2.62 (CONCERN-6 remediation): poison-tolerant `list_projects`
    /// for the hub's detached infra-watchdog task, which must NEVER panic
    /// (a panic in the detached task kills it for the rest of the hub
    /// process, defeating its purpose).
    ///
    /// Identical query to [`Db::list_projects`] but acquires the connection
    /// via [`Db::lock_recover`] (recovers a poisoned mutex instead of
    /// `.expect()`-panicking). All other failure modes still surface as
    /// `Err(String)` for the caller to log + soft-fail.
    pub fn list_projects_nonpanicking(&self) -> Result<Vec<ProjectRow>, String> {
        let guard = self.lock_recover();
        Self::list_projects_with_guard(&guard)
    }

    /// Shared query body for [`Db::list_projects`] +
    /// [`Db::list_projects_nonpanicking`] — they differ only in how they
    /// acquire the lock (panic-on-poison vs recover-on-poison).
    fn list_projects_with_guard(
        guard: &rusqlite::Connection,
    ) -> Result<Vec<ProjectRow>, String> {
        let mut stmt = guard
            .prepare(
                "SELECT id, name, folder_path, host, slug, created_at, updated_at, rl_port
                 FROM projects ORDER BY name ASC",
            )
            .map_err(|e| format!("prepare list: {}", e))?;
        let rows = stmt
            .query_map([], row_to_project)
            .map_err(|e| format!("query list: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list: {}", e))
    }

    // ─── rl_port (migration 014, generalised in 017 / v0.2.26) ───────────
    //
    // B2 / single-writer principle: the RL reranker port is a HUB-writable
    // system-observed value (v0.2.21 Step 3 decision tightening). The
    // launcher GUI does NOT write it; only the supervisor in
    // `vct-hub::module_supervisor` allocates and persists.
    //
    // v0.2.26 generalisation: the source-of-truth moved from the RL-only
    // `projects.rl_port` column (migration 014) to the generic
    // `module_ports` table (migration 017). These wrappers preserve the
    // existing public signature so callers from the hub crate
    // (`module_supervisor::ensure_rl_port_persisted`, `module_service.rs`
    // commands) compile unchanged — they just dispatch into
    // `get_module_port` / `set_module_port` with the canonical RL module
    // id. The `projects.rl_port` column stays in place (migration 017
    // backfills `module_ports` from it on apply); it will be retired in
    // a later migration once every consumer is confirmed off it.

    /// Module id used by the legacy `get_project_rl_port` /
    /// `set_project_rl_port` wrappers — i.e. the canonical id for the
    /// RL reranker container. Lives here (not in `vct-hub`) so the
    /// `vct-launcher-core` tests can reference it.
    pub const RL_RERANKER_MODULE_ID: &'static str = "vct-rl-reranker";

    /// Read the per-project RL reranker server port. Returns `Ok(None)`
    /// when no row exists in `module_ports` for this project (project
    /// predates allocation OR project doesn't exist).
    ///
    /// Thin wrapper around [`Db::get_module_port`] with
    /// `module_id = "vct-rl-reranker"`. Kept for back-compat with the
    /// existing hub callers.
    pub fn get_project_rl_port(&self, project_id: &str) -> Result<Option<u16>, String> {
        self.get_module_port(project_id, Self::RL_RERANKER_MODULE_ID)
    }

    /// Persist the per-project RL reranker server port. HUB-only call
    /// site (see B2 single-writer note above). Caller is responsible for
    /// choosing a value (11442 for orchestrator-root, 11500..=11900
    /// random otherwise) and ensuring no collision.
    ///
    /// Thin wrapper around [`Db::set_module_port`] with
    /// `module_id = "vct-rl-reranker"`. Kept for back-compat with the
    /// existing hub callers.
    pub fn set_project_rl_port(&self, project_id: &str, port: u16) -> Result<(), String> {
        self.set_module_port(project_id, Self::RL_RERANKER_MODULE_ID, port)
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

    // ─── folder_missing_at_last_boot (migration 030, v0.2.49 Phase 6 S-4) ─
    //
    // The launcher's boot sanity check walks every project row, fs::is_dir-
    // checks `folder_path`, and stamps this flag on/off accordingly. The
    // frontend reads the flag via `read_project_folder_missing_flags` and
    // renders a non-blocking warning banner on the affected project card
    // ("Folder not found at <path>. Did you move or delete it?"). The
    // banner is dismissed automatically when the folder reappears on a
    // subsequent boot (the probe re-checks and clears the flag).
    //
    // Soft-fail discipline: this is a UX safety net, not a load-bearing
    // gate. DB errors at any step return the no-op default (empty list /
    // unit Ok) so the launcher boots even when the probe can't run.

    /// Read every project's id, folder_path, and current
    /// `folder_missing_at_last_boot` flag. Used by the boot probe to
    /// decide which rows need updating (set vs clear vs leave alone).
    ///
    /// Returns an empty vec when the DB query fails — the boot probe is
    /// best-effort and must not abort the launcher on a transient DB
    /// hiccup.
    pub fn list_project_folder_paths(&self) -> Result<Vec<(String, String, bool)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, folder_path, folder_missing_at_last_boot
                 FROM projects
                 ORDER BY id ASC",
            )
            .map_err(|e| format!("prepare list folder paths: {}", e))?;
        let rows = stmt
            .query_map([], |row| {
                let id: String = row.get(0)?;
                let folder_path: String = row.get(1)?;
                let flag: i64 = row.get(2)?;
                Ok((id, folder_path, flag != 0))
            })
            .map_err(|e| format!("query list folder paths: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list folder paths: {}", e))
    }

    /// Persist the boot probe's verdict for a single project. Idempotent:
    /// rewriting the same value is a no-op SQL UPDATE (one row, same
    /// content). Does NOT bump `updated_at` — this column is set by the
    /// boot probe (system-observed value), not by the user, and bumping
    /// `updated_at` would falsely mark the row as "recently user-edited"
    /// for any predicate that cares (e.g. the future is_user_configured
    /// audit-trail logic).
    pub fn set_project_folder_missing_flag(
        &self,
        id: &str,
        missing: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let flag_i: i64 = if missing { 1 } else { 0 };
        guard
            .execute(
                "UPDATE projects SET folder_missing_at_last_boot = ?1 WHERE id = ?2",
                params![flag_i, id],
            )
            .map_err(|e| format!("set folder_missing flag: {}", e))?;
        Ok(())
    }

    /// Convenience read: return `true` when the project row's
    /// `folder_missing_at_last_boot` flag is set. Returns
    /// `Ok(false)` for an unknown id (the GUI will already have
    /// filtered it out via `list_projects`); `Err` only on hard
    /// DB failures.
    pub fn get_project_folder_missing_flag(&self, id: &str) -> Result<bool, String> {
        let guard = self.lock();
        let result: Option<i64> = guard
            .query_row(
                "SELECT folder_missing_at_last_boot FROM projects WHERE id = ?1",
                params![id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| format!("get folder_missing flag: {}", e))?;
        Ok(result.map(|v| v != 0).unwrap_or(false))
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
        rl_port: row.get::<_, Option<i64>>(7).unwrap_or(None),
    })
}
