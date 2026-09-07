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

// ═══════════════════════════════════════════════════════════════════════════
// Project MOVE (v0.2.92 WP-17 / W3) — the sanctioned DB side of a folder move
// ═══════════════════════════════════════════════════════════════════════════
//
// A move rewrites a project's path across launcher.db, its `.claude/` state
// and its collection bindings. The FILE side lives in `vco_lib/project_move.py`
// (one engine, driven identically by the CLI and by the launcher). THIS side
// is everything that touches the database, and it lives here — in the core
// crate — so the hub route and the Tauri command call the SAME code instead of
// each growing its own opinion about what a move does to the DB.
//
// ── THE INVARIANT ─────────────────────────────────────────────────────────
// IDENTITY IS ROW-KEYED, NOT PATH-DERIVED. Nothing below re-derives a
// collection name, a code-graph prefix, a slug or a project name from the new
// folder's basename. A move changes where a project LIVES; it must not change
// WHO it is. Deriving identity from a folder name is how this machine ended up
// with prefix files naming classes that do not exist — the project keeps its
// data and loses the ability to find it. `commit_project_move` touches
// `folder_path` and the columns whose values are DERIVED FROM `folder_path`.
// It touches no identity column, and there is a test that says so.
//
// ── WHY ONE TRANSACTION ───────────────────────────────────────────────────
// The field failure was not a bad write; it was a PARTIAL one. `folder_path`
// was flipped correctly by hand and 97 `file_path` rows across
// `project_agents` + `project_skills` kept pointing at the old root, so the
// project was registered in the new place and loaded its agents from the old
// one. Doing the flip and the re-point in separate statements re-creates that
// state on any crash between them. They are one transaction, so the database
// is never in the half-moved state that motivated this whole feature.

/// A live or historical row of `project_moves` (migration 044).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ProjectMoveRow {
    pub id: String,
    pub project_id: String,
    pub src: String,
    pub dst: String,
    pub status: String,
    pub error: Option<String>,
    pub started_at: i64,
    pub flipped_at: Option<i64>,
    pub finished_at: Option<i64>,
}

/// Status values for `project_moves.status`. Mirrors migration 044's CHECK.
pub mod move_status {
    /// Claimed. Files may be copying INTO the destination; NOTHING in
    /// `projects` has changed and the project still works at its old folder.
    pub const RUNNING: &str = "running";
    /// The commit transaction landed. The project lives at the destination;
    /// post-commit reconciliation may still be owed.
    pub const FLIPPED: &str = "flipped";
    /// Reconciliation finished too. Nothing is owed.
    pub const COMPLETED: &str = "completed";
    /// Aborted before the flip. The project never moved.
    pub const FAILED: &str = "failed";
}

/// What `commit_project_move` actually did, for the caller's summary.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct MoveCommitReport {
    pub agents_repointed: usize,
    pub skills_repointed: usize,
    /// `true` only when a pending code-graph build row is actually queued.
    /// The caller uses this to decide whether the ledger entry may honestly
    /// claim "VCO retries this itself".
    pub codegraph_enqueued: bool,
    /// Rows whose recorded file is missing at the destination, or which have a
    /// file on BOTH the enabled and disabled side. Surfaced, never silently
    /// "fixed" by flipping a flag the user set.
    pub reconcile_warnings: Vec<String>,
    /// Rows of `project_kg_bindings` whose `kg_dir_path` was rebased under
    /// the new root (v0.2.92 W14). Zero is the normal case: the column is
    /// NULL unless the GUI or hub set it.
    pub kg_dir_paths_repointed: usize,
}

impl Db {
    /// Flip a project's `folder_path`.
    ///
    /// Standalone because a caller may need only the flip (a repair path, a
    /// test); the MOVE always goes through [`Db::commit_project_move`], which
    /// wraps this and the dependent re-points in one transaction.
    ///
    /// Clears `folder_missing_at_last_boot`: the boot probe set that flag
    /// because the OLD path was gone, and leaving it set would make the new
    /// project card warn about a folder that is present.
    ///
    /// The duplicate refusal is the `folder_path` UNIQUE constraint, not a
    /// pre-flight SELECT. A check-then-act would leave a window in which
    /// another writer registers the same path between the check and the
    /// UPDATE; letting SQLite refuse is race-proof by construction.
    pub fn update_project_folder_path(
        &self,
        id: &str,
        new_path: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        Self::update_project_folder_path_on(&guard, id, new_path)
    }

    /// The flip, against an explicit connection (so it can run inside a
    /// caller's transaction).
    fn update_project_folder_path_on(
        conn: &rusqlite::Connection,
        id: &str,
        new_path: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let n = conn
            .execute(
                "UPDATE projects
                    SET folder_path = ?1,
                        folder_missing_at_last_boot = 0,
                        updated_at = ?2
                  WHERE id = ?3",
                params![new_path, now, id],
            )
            .map_err(|e| {
                // Surface the UNIQUE violation as the actionable sentence
                // rather than as raw SQLite text.
                let raw = e.to_string();
                if raw.contains("UNIQUE") && raw.contains("folder_path") {
                    format!(
                        "another project is already registered at {} \
                         (folder_path is UNIQUE)",
                        new_path
                    )
                } else {
                    format!("update folder_path: {}", raw)
                }
            })?;
        if n == 0 {
            return Err(format!("project {} not found", id));
        }
        Ok(())
    }

    /// The live (`running` / `flipped`) move for a project, if any.
    pub fn live_project_move(
        &self,
        project_id: &str,
    ) -> Result<Option<ProjectMoveRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, project_id, src, dst, status, error, started_at,
                        flipped_at, finished_at
                   FROM project_moves
                  WHERE project_id = ?1 AND status IN ('running','flipped')
                  ORDER BY started_at DESC
                  LIMIT 1",
                params![project_id],
                row_to_project_move,
            )
            .optional()
            .map_err(|e| format!("live_project_move: {}", e))
    }

    /// Read one move row by id.
    pub fn get_project_move(&self, move_id: &str) -> Result<Option<ProjectMoveRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT id, project_id, src, dst, status, error, started_at,
                        flipped_at, finished_at
                   FROM project_moves WHERE id = ?1",
                params![move_id],
                row_to_project_move,
            )
            .optional()
            .map_err(|e| format!("get_project_move: {}", e))
    }

    /// Every live move across all projects — the launcher's boot sweep.
    ///
    /// A `running` row at boot means a move was interrupted BEFORE the flip
    /// (the project is untouched); a `flipped` row means it was interrupted
    /// AFTER (the project moved and reconciliation is owed). The two need
    /// different sentences, so the caller gets the status rather than a bool.
    pub fn list_live_project_moves(&self) -> Result<Vec<ProjectMoveRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, src, dst, status, error, started_at,
                        flipped_at, finished_at
                   FROM project_moves
                  WHERE status IN ('running','flipped')
                  ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list_live_project_moves: {}", e))?;
        let rows = stmt
            .query_map([], row_to_project_move)
            .map_err(|e| format!("query list_live_project_moves: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_live_project_moves: {}", e))
    }

    /// Claim single-flight for a move. Nothing else happens here.
    ///
    /// Called BEFORE the engine copies anything, so a second concurrent move
    /// is refused while the first one's files are still landing. The refusal
    /// comes from migration 044's partial UNIQUE index — SQLite decides, not a
    /// SELECT-then-INSERT that another writer can slip between.
    ///
    /// `stale_after_ms` releases an abandoned claim: a launcher killed mid-move
    /// would otherwise block every future move of that project forever. The
    /// stale row is marked `failed` (a record of what happened), never deleted.
    pub fn begin_project_move(
        &self,
        move_id: &str,
        project_id: &str,
        dst: &str,
        stale_after_ms: i64,
    ) -> Result<ProjectMoveRow, String> {
        let now = Utc::now().timestamp_millis();
        let mut guard = self.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("begin_project_move begin: {}", e))?;

        let src: String = tx
            .query_row(
                "SELECT folder_path FROM projects WHERE id = ?1",
                params![project_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("begin_project_move read project: {}", e))?
            .ok_or_else(|| format!("project {} not found", project_id))?;

        // Retire an abandoned claim before trying to take one. `started_at` is
        // the only liveness signal a crashed process leaves, so the threshold
        // is a time bound rather than a pid probe: a pid check would be wrong
        // across a reboot (pid reuse) and unavailable across a container
        // boundary.
        let live: Option<(String, String, i64)> = tx
            .query_row(
                "SELECT id, status, started_at FROM project_moves
                  WHERE project_id = ?1 AND status IN ('running','flipped')
                  ORDER BY started_at DESC LIMIT 1",
                params![project_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| format!("begin_project_move probe: {}", e))?;

        if let Some((live_id, live_status, started)) = live {
            let age = now.saturating_sub(started);
            if live_status == move_status::FLIPPED {
                // NEVER auto-retire a flipped move, however old. The project
                // has already moved; releasing the claim would let a second
                // move start from a source that is no longer the project's
                // folder. It needs `--verify`, not a new move.
                return Err(format!(
                    "a move of this project already flipped to its new folder \
                     and has not been reconciled (move {}). Run \
                     `vco project move --verify` first.",
                    live_id
                ));
            }
            if age < stale_after_ms {
                return Err(format!(
                    "another move of this project is in progress (move {}, \
                     started {}ms ago)",
                    live_id, age
                ));
            }
            tx.execute(
                "UPDATE project_moves
                    SET status = ?1, error = ?2, finished_at = ?3
                  WHERE id = ?4",
                params![
                    move_status::FAILED,
                    format!(
                        "abandoned: no progress for {}ms; claim released so a \
                         new move could start",
                        age
                    ),
                    now,
                    live_id
                ],
            )
            .map_err(|e| format!("begin_project_move retire stale: {}", e))?;
        }

        tx.execute(
            "INSERT INTO project_moves
                 (id, project_id, src, dst, status, started_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![move_id, project_id, src, dst, move_status::RUNNING, now],
        )
        .map_err(|e| {
            let raw = e.to_string();
            if raw.contains("UNIQUE") {
                "another move of this project is already in progress".to_string()
            } else {
                format!("begin_project_move insert: {}", raw)
            }
        })?;

        tx.commit()
            .map_err(|e| format!("begin_project_move commit: {}", e))?;
        Ok(ProjectMoveRow {
            id: move_id.to_string(),
            project_id: project_id.to_string(),
            src,
            dst: dst.to_string(),
            status: move_status::RUNNING.to_string(),
            error: None,
            started_at: now,
            flipped_at: None,
            finished_at: None,
        })
    }

    /// THE COMMIT. One transaction: flip, re-point, enqueue, mark `flipped`.
    ///
    /// Either the project is entirely at the new folder with every dependent
    /// path column following it, or nothing changed at all. There is no
    /// intermediate state for a crash to leave behind.
    ///
    /// What is re-pointed, and why each is a TARGETED RECOMPUTE rather than a
    /// string replace (see `vco_lib/path_bearing_keys.py` for the full
    /// registry):
    ///
    /// * `project_agents.file_path` / `project_skills.file_path` — recomputed
    ///   from the new root through [`super::project_state::resolve_kind_paths`],
    ///   the SAME oracle `set_enabled_with_fs_move` uses, choosing the enabled
    ///   or disabled side by the row's own `enabled` flag. A `REPLACE(old,new)`
    ///   would look equivalent and is not: it rewrites any occurrence anywhere
    ///   in the value, and it silently does nothing to a row whose path was
    ///   already odd. Recomputing produces the same answer the toggle would.
    /// * `kg_dir_path` on the KG binding rows — rebased only when it is
    ///   non-NULL AND actually under the old root, by
    ///   [`crate::db::bindings_writer::repoint_kg_dir_path`] (the sanctioned
    ///   single-writer home; see the note above `row_to_project_move`).
    ///   `populate_kg_bindings` skips rows that already exist, so nothing
    ///   else can ever fix it.
    ///
    /// What is deliberately NOT touched: identity columns (`collection_name`,
    /// `collection_prefix`, `name`, `slug`), user-owned paths
    /// (`project_codegraph_extra_paths.path`, `project_secret_refs.file_path`)
    /// and historical text (log tails, telemetry payloads, audit details).
    /// The Python sweep reports those; a fixer here would be VCO deciding for
    /// the user or falsifying a record.
    pub fn commit_project_move(
        &self,
        move_id: &str,
        expected_project_id: &str,
    ) -> Result<MoveCommitReport, String> {
        let now = Utc::now().timestamp_millis();
        let (project_id, src, dst, report) = {
            let mut guard = self.lock();
            let tx = guard
                .transaction()
                .map_err(|e| format!("commit_project_move begin: {}", e))?;

            let (project_id, src, dst, status): (String, String, String, String) = tx
                .query_row(
                    "SELECT project_id, src, dst, status FROM project_moves WHERE id = ?1",
                    params![move_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )
                .optional()
                .map_err(|e| format!("commit_project_move read move: {}", e))?
                .ok_or_else(|| format!("move {} not found", move_id))?;

            if project_id != expected_project_id {
                return Err(format!(
                    "move {} belongs to project {}, not {}",
                    move_id, project_id, expected_project_id
                ));
            }
            if status != move_status::RUNNING {
                return Err(format!(
                    "move {} is '{}', not '{}' — only a running move can commit",
                    move_id, status, move_status::RUNNING
                ));
            }

            Self::update_project_folder_path_on(&tx, &project_id, &dst)?;

            let mut report = MoveCommitReport::default();
            let (agents, agent_warnings) =
                repoint_agent_or_skill_paths(&tx, &project_id, &dst, Kind::Agent)?;
            let (skills, skill_warnings) =
                repoint_agent_or_skill_paths(&tx, &project_id, &dst, Kind::Skill)?;
            report.agents_repointed = agents;
            report.skills_repointed = skills;
            report.reconcile_warnings.extend(agent_warnings);
            report.reconcile_warnings.extend(skill_warnings);

            // v0.2.92 W14, closing W3's documented gap. The fixer lives in the
            // sanctioned single-writer home and is CALLED here so it commits
            // or rolls back with the flip — a folder path re-pointed in a
            // separate transaction could survive a rolled-back move and name
            // a folder the project does not live in.
            report.kg_dir_paths_repointed =
                crate::db::bindings_writer::repoint_kg_dir_path(
                    &tx, &project_id, &src, &dst,
                )?;

            // Queue the code-graph rebuild INSIDE the transaction. If it cannot
            // be written the whole commit rolls back rather than leaving a moved
            // project whose ledger claims a rebuild that was never scheduled.
            // A pre-existing pending/running row means one is already queued —
            // that is a success, not a duplicate to force.
            let already_queued: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM code_graph_builds
                      WHERE project_id = ?1 AND status IN ('pending','running')",
                    params![project_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if already_queued > 0 {
                report.codegraph_enqueued = true;
            } else {
                match tx.execute(
                    "INSERT INTO code_graph_builds (project_id, status, started_at)
                     VALUES (?1, 'pending', ?2)
                     ON CONFLICT(project_id) DO UPDATE SET
                         status = 'pending',
                         started_at = excluded.started_at,
                         finished_at = NULL,
                         error_message = NULL",
                    params![project_id, now],
                ) {
                    Ok(_) => report.codegraph_enqueued = true,
                    Err(e) => {
                        return Err(format!(
                            "commit_project_move could not queue the code-graph \
                             rebuild ({}); the move was rolled back rather than \
                             leaving the project moved with a stale code graph \
                             and nothing scheduled to fix it",
                            e
                        ))
                    }
                }
            }

            tx.execute(
                "UPDATE project_moves SET status = ?1, flipped_at = ?2 WHERE id = ?3",
                params![move_status::FLIPPED, now, move_id],
            )
            .map_err(|e| format!("commit_project_move mark flipped: {}", e))?;

            tx.commit()
                .map_err(|e| format!("commit_project_move commit: {}", e))?;
            (project_id, src, dst, report)
        };

        // Audit + change-log AFTER the transaction (and after the lock is
        // released — both take the lock themselves): they are observability,
        // and an audit hiccup must never roll back a completed move.
        let _ = self.audit(
            "project_path_change",
            Some(&project_id),
            None,
            &serde_json::json!({
                "old": src,
                "new": dst,
                "move_id": move_id,
                "agents_repointed": report.agents_repointed,
                "skills_repointed": report.skills_repointed,
            }),
        );
        let _ = self.log_change("projects", "update", Some(&project_id), Some(&project_id));
        Ok(report)
    }

    /// Mark a flipped move complete: the post-commit reconciliation ran.
    ///
    /// Separate from the commit ON PURPOSE. If this were folded into the
    /// commit, a crash during reconciliation would leave a `completed` row and
    /// the owed work would be invisible. `flipped` is the state that says
    /// "moved, not yet reconciled", and it is the state the boot sweep reports.
    pub fn finish_project_move(&self, move_id: &str) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_moves
                    SET status = ?1, finished_at = ?2
                  WHERE id = ?3 AND status = ?4",
                params![move_status::COMPLETED, now, move_id, move_status::FLIPPED],
            )
            .map_err(|e| format!("finish_project_move: {}", e))?;
        if n == 0 {
            return Err(format!(
                "move {} is not in '{}' — only a flipped move can finish",
                move_id,
                move_status::FLIPPED
            ));
        }
        Ok(())
    }

    /// Release a claim that never flipped.
    ///
    /// Refuses a flipped move: the project HAS moved, and calling that
    /// "failed" would tell the next reader the opposite of what happened.
    pub fn fail_project_move(&self, move_id: &str, error: &str) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_moves
                    SET status = ?1, error = ?2, finished_at = ?3
                  WHERE id = ?4 AND status = ?5",
                params![
                    move_status::FAILED,
                    error.chars().take(2000).collect::<String>(),
                    now,
                    move_id,
                    move_status::RUNNING
                ],
            )
            .map_err(|e| format!("fail_project_move: {}", e))?;
        if n == 0 {
            return Err(format!(
                "move {} is not in '{}' — a flipped move cannot be marked failed",
                move_id,
                move_status::RUNNING
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
enum Kind {
    Agent,
    Skill,
}

impl Kind {
    fn table(self) -> &'static str {
        match self {
            Kind::Agent => "project_agents",
            Kind::Skill => "project_skills",
        }
    }
    fn name_column(self) -> &'static str {
        match self {
            Kind::Agent => "agent_name",
            Kind::Skill => "skill_name",
        }
    }
    fn as_state(self) -> super::project_state::AgentOrSkill {
        match self {
            Kind::Agent => super::project_state::AgentOrSkill::Agent,
            Kind::Skill => super::project_state::AgentOrSkill::Skill,
        }
    }
}

/// Recompute every agent/skill row's `file_path` from the NEW root.
///
/// Covers ENABLED and DISABLED rows with one rule, which is the point. The
/// launcher's `populate_project_state_from_filesystem` re-derives enabled rows
/// correctly and can NEVER reach a disabled one: it scans only the enabled
/// directories and skips any name whose `.disabled/` sibling exists. The
/// disable toggle, for its part, moves the file and flips the flag without
/// ever rewriting `file_path`. So a disabled agent's row is unreachable by
/// both mechanisms — which is exactly why 97 rows survived the field move
/// pointing at the old root, and why this function asks the path ORACLE
/// instead of asking the filesystem what it can see.
///
/// Existence is CHECKED and reported, never acted on: a row whose file is
/// missing at the destination, or which has a file on both sides, produces a
/// warning for the caller to surface. Flipping `enabled` here would be VCO
/// silently overriding a choice the user made in the GUI.
fn repoint_agent_or_skill_paths(
    conn: &rusqlite::Connection,
    project_id: &str,
    new_root: &str,
    kind: Kind,
) -> Result<(usize, Vec<String>), String> {
    let select = format!(
        "SELECT {name}, enabled, file_path FROM {table} WHERE project_id = ?1",
        name = kind.name_column(),
        table = kind.table()
    );
    let mut rows: Vec<(String, bool, Option<String>)> = Vec::new();
    {
        let mut stmt = conn
            .prepare(&select)
            .map_err(|e| format!("prepare repoint {}: {}", kind.table(), e))?;
        let iter = stmt
            .query_map(params![project_id], |r| {
                let name: String = r.get(0)?;
                let enabled: i64 = r.get(1)?;
                let path: Option<String> = r.get(2)?;
                Ok((name, enabled != 0, path))
            })
            .map_err(|e| format!("query repoint {}: {}", kind.table(), e))?;
        for row in iter {
            rows.push(row.map_err(|e| format!("row repoint {}: {}", kind.table(), e))?);
        }
    }

    let root = std::path::Path::new(new_root);
    let update = format!(
        "UPDATE {table} SET file_path = ?1, updated_at = ?2 \
          WHERE project_id = ?3 AND {name} = ?4",
        table = kind.table(),
        name = kind.name_column()
    );
    let now = Utc::now().timestamp_millis();
    let mut changed = 0usize;
    let mut warnings: Vec<String> = Vec::new();

    for (name, enabled, old_path) in rows {
        let (enabled_path, disabled_path) =
            super::project_state::resolve_kind_paths(root, &name, kind.as_state());
        let want = if enabled { &enabled_path } else { &disabled_path };
        let want_str = want.display().to_string();

        // Report-only reconciliation. `exists()` covers both an agent's file
        // and a skill's directory.
        let on_enabled = enabled_path.exists();
        let on_disabled = disabled_path.exists();
        if on_enabled && on_disabled {
            warnings.push(format!(
                "{} '{}' exists on BOTH the enabled and disabled side at the \
                 new folder; the database flag was left as it was",
                kind.table(),
                name
            ));
        } else if !want.exists() {
            warnings.push(format!(
                "{} '{}' is recorded as {} but no file is at {} — the row was \
                 re-pointed anyway so it stops naming the old folder",
                kind.table(),
                name,
                if enabled { "enabled" } else { "disabled" },
                want_str
            ));
        }

        if old_path.as_deref() == Some(want_str.as_str()) {
            continue;
        }
        conn.execute(&update, params![want_str, now, project_id, name])
            .map_err(|e| format!("update repoint {}: {}", kind.table(), e))?;
        changed += 1;
    }
    Ok((changed, warnings))
}

/// NOT IMPLEMENTED HERE, DELIBERATELY: rebasing the KG bindings'
/// `kg_dir_path` — it lives in
/// [`crate::db::bindings_writer::repoint_kg_dir_path`] and is CALLED from
/// `commit_project_move` above, inside the existing transaction.
///
/// The reason it is not written in this file: an UPDATE against the
/// KG-bindings table from `projects.rs` trips the SINGLE-WRITER gate.
/// (The literal table name is deliberately not spelled next to a SQL verb in
/// this comment — the single-writer scanner matches source TEXT and cannot
/// tell prose from code, so a comment explaining the rule would trip it.)
/// `tests/test_kg_binding_single_writer_rust.py` allows binding SQL only in
/// `db/{bindings_writer,project_state,access,migrations}.rs`. That gate
/// exists because binding rows have been corrupted by ad-hoc writers before,
/// and evading it — by building the statement dynamically, or by hiding the
/// literal in another file — would be worse than not writing the column at
/// all.
///
/// v0.2.92 W3 therefore classified the column `sweep-only` and left the
/// recipe; W14 implemented it in the sanctioned home and flipped the policy
/// to `targeted-update`, which the parity test
/// (`test_every_targeted_update_column_has_a_fixer_in_the_rust_writer`) now
/// REQUIRES. Refusing to route around the gate, and then closing it properly,
/// is the sequence this comment records.

fn row_to_project_move(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProjectMoveRow> {
    Ok(ProjectMoveRow {
        id: row.get(0)?,
        project_id: row.get(1)?,
        src: row.get(2)?,
        dst: row.get(3)?,
        status: row.get(4)?,
        error: row.get(5)?,
        started_at: row.get(6)?,
        flipped_at: row.get(7)?,
        finished_at: row.get(8)?,
    })
}

// ═══════════════════════════════════════════════════════════════════════════
// Tests — the DB side of a project move (v0.2.92 WP-17 / W3)
// ═══════════════════════════════════════════════════════════════════════════
//
// Both sides of every destructive-capable step. The leave-alone side is the
// one that matters: the field failure was a PARTIAL write, and a test suite
// that only proves the intended rows changed cannot tell a correct move from
// one that also clobbered a neighbour.

#[cfg(test)]
mod move_tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn tmpdir() -> tempfile::TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    fn seed(db: &Db, id: &str, folder: &str) {
        db.insert_project(id, id, folder, ProjectHost::Base, id)
            .expect("insert project");
    }

    fn agent(db: &Db, project: &str, name: &str, enabled: bool, path: &str) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO project_agents (project_id, agent_name, source, enabled,
                                             file_path, installed_at, updated_at)
                 VALUES (?1, ?2, 'bundled', ?3, ?4, 1, 1)",
                params![project, name, if enabled { 1 } else { 0 }, path],
            )
            .expect("insert agent");
    }

    fn skill(db: &Db, project: &str, name: &str, enabled: bool, path: &str) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO project_skills (project_id, skill_name, source, enabled,
                                             file_path, installed_at, updated_at)
                 VALUES (?1, ?2, 'bundled', ?3, ?4, 1, 1)",
                params![project, name, if enabled { 1 } else { 0 }, path],
            )
            .expect("insert skill");
    }

    fn agent_path(db: &Db, project: &str, name: &str) -> Option<String> {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT file_path FROM project_agents WHERE project_id=?1 AND agent_name=?2",
                params![project, name],
                |r| r.get(0),
            )
            .optional()
            .unwrap()
            .flatten()
    }

    fn agent_enabled(db: &Db, project: &str, name: &str) -> i64 {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT enabled FROM project_agents WHERE project_id=?1 AND agent_name=?2",
                params![project, name],
                |r| r.get(0),
            )
            .unwrap()
    }

    fn folder_of(db: &Db, id: &str) -> String {
        db.get_project(id).unwrap().unwrap().folder_path
    }

    /// Materialise `<root>/.claude/agents/<name>.md` (or the disabled sibling).
    fn touch_agent_file(root: &PathBuf, name: &str, enabled: bool) {
        let dir = root
            .join(".claude")
            .join(if enabled { "agents" } else { "agents.disabled" });
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(format!("{}.md", name)), "x").unwrap();
    }

    // ───── update_project_folder_path ─────

    #[test]
    fn flip_updates_the_row_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.update_project_folder_path("p1", "/w/new").unwrap();
        assert_eq!(folder_of(&db, "p1"), "/w/new");
    }

    #[test]
    fn flip_leaves_another_projects_row_byte_identical_leaves_alone() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        let before = db.get_project("p2").unwrap().unwrap();

        db.update_project_folder_path("p1", "/w/moved").unwrap();

        let after = db.get_project("p2").unwrap().unwrap();
        assert_eq!(before.folder_path, after.folder_path);
        assert_eq!(before.name, after.name);
        assert_eq!(before.slug, after.slug);
        assert_eq!(
            before.updated_at, after.updated_at,
            "an unrelated project's row must not even have its timestamp moved"
        );
    }

    #[test]
    fn flip_refuses_a_path_another_project_already_holds_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");

        let err = db
            .update_project_folder_path("p1", "/w/two")
            .expect_err("the UNIQUE constraint must refuse this");
        assert!(
            err.contains("already registered"),
            "the refusal must name the cause in words the user can act on, \
             got: {}",
            err
        );
        assert_eq!(
            folder_of(&db, "p1"),
            "/w/one",
            "a refused flip leaves the row exactly as it was"
        );
    }

    #[test]
    fn flip_clears_the_folder_missing_flag() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/gone");
        db.set_project_folder_missing_flag("p1", true).unwrap();
        assert!(db.get_project_folder_missing_flag("p1").unwrap());

        db.update_project_folder_path("p1", "/w/here").unwrap();
        assert!(
            !db.get_project_folder_missing_flag("p1").unwrap(),
            "the flag was set because the OLD path was gone; leaving it would \
             warn about a folder that is present"
        );
    }

    #[test]
    fn flip_refuses_an_unknown_project() {
        let db = Db::open_in_memory().unwrap();
        assert!(db.update_project_folder_path("nope", "/w/x").is_err());
    }

    // ───── begin / single-flight ─────

    #[test]
    fn begin_claims_and_records_the_current_folder_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        let row = db.begin_project_move("mv1", "p1", "/w/new", 3600_000).unwrap();
        assert_eq!(row.src, "/w/old", "the claim records where the project WAS");
        assert_eq!(row.dst, "/w/new");
        assert_eq!(row.status, move_status::RUNNING);
        assert_eq!(
            folder_of(&db, "p1"),
            "/w/old",
            "claiming changes NOTHING about the project — that is what makes a \
             pre-commit failure a clean refusal"
        );
    }

    #[test]
    fn begin_refuses_a_second_live_move_of_the_same_project_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", "/w/a", 3600_000).unwrap();
        let err = db
            .begin_project_move("mv2", "p1", "/w/b", 3600_000)
            .expect_err("single-flight");
        assert!(err.contains("in progress"), "got: {}", err);
    }

    #[test]
    fn begin_allows_a_concurrent_move_of_a_different_project_leaves_alone() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        db.begin_project_move("mv1", "p1", "/w/a", 3600_000).unwrap();
        db.begin_project_move("mv2", "p2", "/w/b", 3600_000)
            .expect("single-flight is PER PROJECT, not global");
    }

    #[test]
    fn begin_retires_an_abandoned_running_claim_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", "/w/a", 3600_000).unwrap();
        // Age the claim past the threshold.
        {
            let guard = db.lock();
            guard
                .execute(
                    "UPDATE project_moves SET started_at = 0 WHERE id='mv1'",
                    [],
                )
                .unwrap();
        }
        db.begin_project_move("mv2", "p1", "/w/b", 1000)
            .expect("an abandoned claim must not block every future move");

        let old = db.get_project_move("mv1").unwrap().unwrap();
        assert_eq!(
            old.status,
            move_status::FAILED,
            "the retired claim is RECORDED as failed, never deleted"
        );
        assert!(old.error.unwrap().contains("abandoned"));
    }

    #[test]
    fn begin_never_retires_a_flipped_move_however_old_leaves_alone() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", "/w/new", 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();
        {
            let guard = db.lock();
            guard
                .execute("UPDATE project_moves SET started_at = 0 WHERE id='mv1'", [])
                .unwrap();
        }
        let err = db
            .begin_project_move("mv2", "p1", "/w/other", 1)
            .expect_err("a flipped move must never be auto-retired");
        assert!(err.contains("--verify"), "the refusal names the remedy: {}", err);

        let row = db.get_project_move("mv1").unwrap().unwrap();
        assert_eq!(row.status, move_status::FLIPPED);
    }

    // ───── commit ─────

    #[test]
    fn commit_flips_and_repoints_in_one_step_act() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "planner", true);
        touch_agent_file(&new_root, "archived", false);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        agent(&db, "p1", "planner", true, "/w/old/.claude/agents/planner.md");
        agent(
            &db,
            "p1",
            "archived",
            false,
            "/w/old/.claude/agents/archived.md",
        );
        skill(&db, "p1", "tdd", true, "/w/old/.claude/skills/tdd");

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        let report = db.commit_project_move("mv1", "p1").unwrap();

        assert_eq!(folder_of(&db, "p1"), new_root_s);
        assert_eq!(report.agents_repointed, 2);
        assert_eq!(report.skills_repointed, 1);

        let planner = agent_path(&db, "p1", "planner").unwrap();
        assert!(
            planner.starts_with(&new_root_s) && planner.ends_with("planner.md"),
            "enabled row re-pointed into the new root: {}",
            planner
        );
    }

    #[test]
    fn commit_repoints_a_disabled_row_to_the_disabled_side_act() {
        // The row populate can NEVER reach: it scans only the enabled dirs,
        // and the enable-toggle never rewrites file_path. 97 rows survived the
        // field move for exactly this reason.
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "archived", false);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        agent(
            &db,
            "p1",
            "archived",
            false,
            "/w/old/.claude/agents.disabled/archived.md",
        );

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        let p = agent_path(&db, "p1", "archived").unwrap();
        assert!(
            p.contains("agents.disabled"),
            "a disabled row must point at the DISABLED side: {}",
            p
        );
        assert!(p.starts_with(&new_root_s), "and at the NEW root: {}", p);
    }

    #[test]
    fn commit_preserves_the_enabled_flag_leaves_alone() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "on", true);
        touch_agent_file(&new_root, "off", false);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        agent(&db, "p1", "on", true, "/w/old/.claude/agents/on.md");
        agent(&db, "p1", "off", false, "/w/old/.claude/agents.disabled/off.md");

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        assert_eq!(agent_enabled(&db, "p1", "on"), 1);
        assert_eq!(
            agent_enabled(&db, "p1", "off"),
            0,
            "the user's disable choice survives the move"
        );
    }

    #[test]
    fn commit_reports_a_both_sides_present_row_instead_of_flipping_a_flag() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "ambiguous", true);
        touch_agent_file(&new_root, "ambiguous", false);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        agent(
            &db,
            "p1",
            "ambiguous",
            false,
            "/w/old/.claude/agents.disabled/ambiguous.md",
        );

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        let report = db.commit_project_move("mv1", "p1").unwrap();

        assert!(
            report
                .reconcile_warnings
                .iter()
                .any(|w| w.contains("BOTH")),
            "an ambiguous pair is SURFACED: {:?}",
            report.reconcile_warnings
        );
        assert_eq!(
            agent_enabled(&db, "p1", "ambiguous"),
            0,
            "and the flag the user set is left exactly as it was"
        );
    }

    #[test]
    fn commit_leaves_another_projects_rows_byte_identical_leaves_alone() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "mine", true);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        agent(&db, "p1", "mine", true, "/w/one/.claude/agents/mine.md");
        agent(&db, "p2", "theirs", true, "/w/two/.claude/agents/theirs.md");

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        assert_eq!(
            agent_path(&db, "p2", "theirs").unwrap(),
            "/w/two/.claude/agents/theirs.md",
            "the other project's agent row must be untouched"
        );
        assert_eq!(folder_of(&db, "p2"), "/w/two");
    }

    #[test]
    fn commit_never_touches_identity_columns_leaves_alone() {
        // THE invariant. A move changes where a project LIVES, not who it is.
        let td = tmpdir();
        let new_root = td.path().join("brand-new-basename");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings (project_id, role, collection_name, updated_at)
                     VALUES ('p1','primary','OriginalName_KnowledgeGraph', 1)",
                    [],
                )
                .unwrap();
            guard
                .execute(
                    "INSERT INTO project_codegraph_bindings (project_id, collection_prefix, updated_at)
                     VALUES ('p1','OriginalName', 1)",
                    [],
                )
                .unwrap();
        }

        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        let guard = db.lock();
        let kg: String = guard
            .query_row(
                "SELECT collection_name FROM project_kg_bindings WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let cg: String = guard
            .query_row(
                "SELECT collection_prefix FROM project_codegraph_bindings WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(guard);
        assert_eq!(
            kg, "OriginalName_KnowledgeGraph",
            "re-deriving the collection name from the new folder basename is \
             how a project keeps its data and loses the ability to find it"
        );
        assert_eq!(cg, "OriginalName");

        let row = db.get_project("p1").unwrap().unwrap();
        assert_eq!(row.name, "p1", "the display name is not path-derived");
        assert_eq!(row.slug, "p1", "the slug is not path-derived");
    }

    #[test]
    fn commit_rebases_kg_dir_path_under_the_new_root() {
        // v0.2.92 W14 CHANGED THIS BEHAVIOUR DELIBERATELY. W3 classified the
        // column `sweep-only` and pinned "left exactly as it was", because
        // writing binding SQL from this file trips the single-writer gate.
        // W14 wrote the fixer in the sanctioned home
        // (`db::bindings_writer::repoint_kg_dir_path`) and calls it from
        // inside this transaction, so the column is now `targeted-update` and
        // the parity test REQUIRES a fixer. The old assertion is not deleted
        // as an inconvenience — it is replaced by the pair below, which pins
        // both halves of the new contract.
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                       (project_id, role, collection_name, kg_dir_path, updated_at)
                     VALUES ('p1','primary','C', '/w/old/knowledge', 1)",
                    [],
                )
                .unwrap();
        }
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        let report = db.commit_project_move("mv1", "p1").unwrap();
        assert_eq!(report.kg_dir_paths_repointed, 1);

        let guard = db.lock();
        let dir: String = guard
            .query_row(
                "SELECT kg_dir_path FROM project_kg_bindings WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(guard);
        assert_eq!(
            dir,
            format!("{}/knowledge", new_root_s.trim_end_matches('/')),
            "the value under the old root follows the move"
        );
    }

    #[test]
    fn commit_leaves_a_kg_dir_path_outside_the_old_root_alone_leaves_alone() {
        // The leave-alone half. A pointer at a directory OUTSIDE the project
        // is the user's deliberate choice; rebasing it would be VCO deciding
        // for them, and it is also how a component-blind `REPLACE` corrupts a
        // sibling path.
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                       (project_id, role, collection_name, kg_dir_path, updated_at)
                     VALUES ('p1','primary','C', '/somewhere/else/knowledge', 1)",
                    [],
                )
                .unwrap();
        }
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        let report = db.commit_project_move("mv1", "p1").unwrap();
        assert_eq!(report.kg_dir_paths_repointed, 0);

        let guard = db.lock();
        let dir: String = guard
            .query_row(
                "SELECT kg_dir_path FROM project_kg_bindings WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(guard);
        assert_eq!(dir, "/somewhere/else/knowledge");
    }

    #[test]
    fn commit_leaves_user_owned_extra_codegraph_paths_alone_leaves_alone() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_codegraph_extra_paths
                       (project_id, path, label, added_at)
                     VALUES ('p1','/w/old/vendor/clone','vendor', 1)",
                    [],
                )
                .unwrap();
        }
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        let guard = db.lock();
        let p: String = guard
            .query_row(
                "SELECT path FROM project_codegraph_extra_paths WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(guard);
        assert_eq!(
            p, "/w/old/vendor/clone",
            "the user may deliberately want the old clone indexed; only they \
             know, so only they re-point it"
        );
    }

    #[test]
    fn commit_queues_the_codegraph_rebuild_act() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        let report = db.commit_project_move("mv1", "p1").unwrap();
        assert!(
            report.codegraph_enqueued,
            "the ledger may only claim 'VCO retries this itself' when work was \
             actually scheduled"
        );

        let guard = db.lock();
        let status: String = guard
            .query_row(
                "SELECT status FROM code_graph_builds WHERE project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(guard);
        assert_eq!(status, "pending");
    }

    #[test]
    fn commit_refuses_a_move_that_is_not_running() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();
        let err = db
            .commit_project_move("mv1", "p1")
            .expect_err("a flipped move must not commit twice");
        assert!(err.contains("only a running move"), "got: {}", err);
    }

    #[test]
    fn commit_refuses_a_move_belonging_to_another_project() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        db.begin_project_move("mv1", "p1", "/w/a", 3600_000).unwrap();
        assert!(db.commit_project_move("mv1", "p2").is_err());
    }

    #[test]
    fn commit_rolls_everything_back_when_the_flip_collides() {
        // THE interrupted-case proof: the flip fails on the UNIQUE constraint
        // AFTER the transaction has already opened, so the re-points and the
        // build enqueue must vanish with it. A partial commit here would be
        // precisely the half-moved state this design exists to make
        // impossible.
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        agent(&db, "p1", "planner", true, "/w/one/.claude/agents/planner.md");

        db.begin_project_move("mv1", "p1", "/w/two", 3600_000).unwrap();
        let err = db
            .commit_project_move("mv1", "p1")
            .expect_err("the destination is another project's folder");
        assert!(err.contains("already registered"), "got: {}", err);

        assert_eq!(folder_of(&db, "p1"), "/w/one", "the flip rolled back");
        assert_eq!(
            agent_path(&db, "p1", "planner").unwrap(),
            "/w/one/.claude/agents/planner.md",
            "the re-point rolled back WITH it — that is the whole point of one \
             transaction"
        );
        let guard = db.lock();
        let builds: i64 = guard
            .query_row("SELECT COUNT(*) FROM code_graph_builds", [], |r| r.get(0))
            .unwrap();
        drop(guard);
        assert_eq!(builds, 0, "and so did the queued rebuild");

        let row = db.get_project_move("mv1").unwrap().unwrap();
        assert_eq!(
            row.status,
            move_status::RUNNING,
            "the claim survives so the caller can abort it explicitly"
        );
    }

    #[test]
    fn commit_rolls_back_a_flip_that_already_succeeded_when_a_later_step_fails() {
        // THE INTERRUPTED-CASE PROOF, and the one that actually bites.
        //
        // Its sibling above (`..._when_the_flip_collides`) fails at the FIRST
        // statement, so it would pass even without a transaction — there is
        // nothing partial to roll back. This one fails at the LAST statement,
        // AFTER the flip and both re-points have already been applied inside
        // the transaction. Only a real rollback can restore the row here, so
        // this is the test that proves the atomicity claim rather than
        // restating it.
        //
        // The late failure is produced by removing the table the enqueue step
        // writes, which is deterministic and needs no fault injection.
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        touch_agent_file(&new_root, "planner", true);

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        agent(&db, "p1", "planner", true, "/w/old/.claude/agents/planner.md");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();

        {
            let guard = db.lock();
            guard
                .execute("DROP TABLE code_graph_builds", [])
                .expect("drop the table the enqueue step needs");
        }

        let err = db
            .commit_project_move("mv1", "p1")
            .expect_err("the enqueue step must fail");
        assert!(
            err.contains("code-graph rebuild"),
            "the error must name what could not be scheduled: {}",
            err
        );

        assert_eq!(
            folder_of(&db, "p1"),
            "/w/old",
            "the flip HAD already been applied inside the transaction; only a \
             rollback restores it"
        );
        assert_eq!(
            agent_path(&db, "p1", "planner").unwrap(),
            "/w/old/.claude/agents/planner.md",
            "the re-point HAD already been applied; it must roll back WITH the \
             flip — a project registered at the new folder whose agent rows \
             still name the old one is the exact half-moved state this design \
             exists to make impossible"
        );
        assert_eq!(
            db.get_project_move("mv1").unwrap().unwrap().status,
            move_status::RUNNING,
            "and the move was never marked flipped"
        );
    }

    // ───── finish / abort ─────

    #[test]
    fn finish_marks_a_flipped_move_completed_act() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();
        db.finish_project_move("mv1").unwrap();
        assert_eq!(
            db.get_project_move("mv1").unwrap().unwrap().status,
            move_status::COMPLETED
        );
    }

    #[test]
    fn finish_refuses_a_move_that_never_flipped_leaves_alone() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", "/w/new", 3600_000).unwrap();
        assert!(
            db.finish_project_move("mv1").is_err(),
            "marking an unflipped move complete would hide owed work"
        );
        assert_eq!(
            db.get_project_move("mv1").unwrap().unwrap().status,
            move_status::RUNNING
        );
    }

    #[test]
    fn abort_releases_a_running_claim_act() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", "/w/new", 3600_000).unwrap();
        db.fail_project_move("mv1", "the copy step failed").unwrap();

        let row = db.get_project_move("mv1").unwrap().unwrap();
        assert_eq!(row.status, move_status::FAILED);
        assert_eq!(row.error.unwrap(), "the copy step failed");
        // And a new move may now be claimed.
        db.begin_project_move("mv2", "p1", "/w/other", 3600_000)
            .expect("the claim was released");
    }

    #[test]
    fn abort_refuses_a_flipped_move_leaves_alone() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/old");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();

        assert!(
            db.fail_project_move("mv1", "oops").is_err(),
            "the project HAS moved; recording that as 'failed' tells the next \
             reader the opposite of what happened"
        );
        assert_eq!(
            db.get_project_move("mv1").unwrap().unwrap().status,
            move_status::FLIPPED
        );
    }

    #[test]
    fn live_moves_report_running_and_flipped_only() {
        let td = tmpdir();
        let new_root = td.path().join("new");
        let new_root_s = new_root.to_string_lossy().to_string();
        fs::create_dir_all(&new_root).unwrap();

        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", "/w/one");
        seed(&db, "p2", "/w/two");
        db.begin_project_move("mv1", "p1", &new_root_s, 3600_000).unwrap();
        db.commit_project_move("mv1", "p1").unwrap();
        db.finish_project_move("mv1").unwrap();
        db.begin_project_move("mv2", "p2", "/w/x", 3600_000).unwrap();

        let live = db.list_live_project_moves().unwrap();
        assert_eq!(live.len(), 1, "a completed move is history, not live");
        assert_eq!(live[0].id, "mv2");
    }

}
