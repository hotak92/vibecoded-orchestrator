// SPDX-License-Identifier: AGPL-3.0-or-later
//! Generic per-(project × module) HTTP port table — `module_ports`
//! (migration 017, v0.2.26).
//!
//! Replaces the RL-only `projects.rl_port` column (migration 014) as the
//! source of truth for where a module's container is reachable. Backs the
//! declarative HTTP-action dispatcher: when a manifest's `gui.config_tab`
//! references an `ActionDescriptor::Http`, the dispatcher resolves
//! `(project_id, module_id) → port` via these helpers before issuing the
//! request.
//!
//! Single-writer principle: the supervisor in `vct-hub::module_supervisor`
//! owns the write path (`set_module_port` / `ensure_module_port`). The
//! launcher GUI does NOT write to this table directly; controls that need
//! a port read via `get_module_port`. The legacy
//! `get_project_rl_port` / `set_project_rl_port` helpers in
//! `db/projects.rs` are now thin wrappers around this module with
//! `module_id = "vct-rl-reranker"`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::Db;

impl Db {
    /// Read a module's port for a project. Returns `Ok(None)` when no
    /// row exists (module not yet allocated, or wasn't part of the
    /// migration-014 backfill).
    pub fn get_module_port(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Option<u16>, String> {
        let guard = self.lock();
        let raw: Option<i64> = guard
            .query_row(
                "SELECT port FROM module_ports
                  WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| format!("get module_port: {}", e))?;
        Ok(raw.and_then(|v| u16::try_from(v).ok()))
    }

    /// Persist a module's port for a project. UPSERT semantics — updates
    /// the row when it already exists. HUB-only write path (see
    /// single-writer note at top of file).
    ///
    /// The FK `project_id → projects.id ON DELETE CASCADE` ensures the
    /// row disappears with the project. Caller is responsible for picking
    /// a non-colliding port; we do not validate against other allocations
    /// here (the supervisor's port-picker logic owns that).
    pub fn set_module_port(
        &self,
        project_id: &str,
        module_id: &str,
        port: u16,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_ports (project_id, module_id, port, updated_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_id) DO UPDATE SET
                    port = excluded.port,
                    updated_at = excluded.updated_at",
                params![project_id, module_id, port as i64, now],
            )
            .map_err(|e| format!("set module_port: {}", e))?;
        Ok(())
    }

    /// Read-or-allocate: returns the existing port if present, otherwise
    /// invokes `allocator` to choose a fresh value, persists it, and
    /// returns it.
    ///
    /// The allocator closure is intentionally a `FnOnce` — it runs at
    /// most once per call and only when the row is absent. Idempotent for
    /// callers: re-invoking returns the SAME port without re-calling the
    /// allocator (verified by the unit tests).
    ///
    /// The read + write are NOT atomic; two concurrent calls for the
    /// same `(project, module)` could both decide to allocate. In
    /// practice the supervisor (`vct-hub::module_supervisor`) is the
    /// sole writer and serialises its own allocations, so this is fine.
    /// If a true race ever becomes a concern, switch to a single
    /// `INSERT ... ON CONFLICT DO NOTHING RETURNING port` query —
    /// rusqlite supports it via `query_row` once the `RETURNING` clause
    /// is wired in. Out of scope for v0.2.26.
    pub fn ensure_module_port<F: FnOnce() -> u16>(
        &self,
        project_id: &str,
        module_id: &str,
        allocator: F,
    ) -> Result<u16, String> {
        if let Some(existing) = self.get_module_port(project_id, module_id)? {
            return Ok(existing);
        }
        let allocated = allocator();
        self.set_module_port(project_id, module_id, allocated)?;
        Ok(allocated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    /// Build a Db with one project row so the FK on `module_ports`
    /// resolves. Returns (db, project_id).
    fn open_db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "test-proj-001".to_string();
        db.insert_project(
            &id,
            "Test Project",
            "/tmp/test-proj-001",
            ProjectHost::Base,
            "test-project",
        )
        .expect("insert project");
        (db, id)
    }

    /// Empty read on a fresh project returns `None`, never an error.
    #[test]
    fn empty_read_returns_none() {
        let (db, pid) = open_db_with_project();
        assert_eq!(db.get_module_port(&pid, "vct-rl-reranker").unwrap(), None);
        assert_eq!(db.get_module_port(&pid, "any-other-module").unwrap(), None);
    }

    /// Round-trip: set then read returns the same value.
    #[test]
    fn set_then_get_roundtrips() {
        let (db, pid) = open_db_with_project();
        db.set_module_port(&pid, "vct-rl-reranker", 11500).unwrap();
        assert_eq!(
            db.get_module_port(&pid, "vct-rl-reranker").unwrap(),
            Some(11500),
        );
    }

    /// Setting the same (project, module) twice updates rather than
    /// inserting — the UPSERT clause must overwrite the previous row.
    #[test]
    fn set_twice_updates_existing_row() {
        let (db, pid) = open_db_with_project();
        db.set_module_port(&pid, "vct-rl-reranker", 11500).unwrap();
        db.set_module_port(&pid, "vct-rl-reranker", 11600).unwrap();
        assert_eq!(
            db.get_module_port(&pid, "vct-rl-reranker").unwrap(),
            Some(11600),
        );
    }

    /// Two different modules in the same project keep independent ports.
    #[test]
    fn different_modules_keep_independent_ports() {
        let (db, pid) = open_db_with_project();
        db.set_module_port(&pid, "vct-rl-reranker", 11500).unwrap();
        db.set_module_port(&pid, "vct-coordination", 11700).unwrap();
        assert_eq!(
            db.get_module_port(&pid, "vct-rl-reranker").unwrap(),
            Some(11500),
        );
        assert_eq!(
            db.get_module_port(&pid, "vct-coordination").unwrap(),
            Some(11700),
        );
    }

    /// `ensure_module_port` allocates a fresh value when no row exists,
    /// and the allocator's value is persisted.
    #[test]
    fn ensure_module_port_allocates_when_missing() {
        let (db, pid) = open_db_with_project();
        let allocated = db
            .ensure_module_port(&pid, "vct-rl-reranker", || 11444)
            .unwrap();
        assert_eq!(allocated, 11444);
        // Subsequent read confirms persistence.
        assert_eq!(
            db.get_module_port(&pid, "vct-rl-reranker").unwrap(),
            Some(11444),
        );
    }

    /// `ensure_module_port` reads the existing value when present,
    /// returning it without invoking the allocator.
    #[test]
    fn ensure_module_port_reads_when_present() {
        let (db, pid) = open_db_with_project();
        db.set_module_port(&pid, "vct-rl-reranker", 11500).unwrap();

        let returned = db
            .ensure_module_port(&pid, "vct-rl-reranker", || {
                panic!("allocator must NOT run when a row already exists")
            })
            .unwrap();
        assert_eq!(returned, 11500);
    }

    /// `ensure_module_port` does NOT invoke the allocator twice across
    /// two calls with the same row present — second call short-circuits.
    #[test]
    fn ensure_module_port_idempotent_across_calls() {
        let (db, pid) = open_db_with_project();
        // First call: allocates 11442.
        let first = db
            .ensure_module_port(&pid, "vct-rl-reranker", || 11442)
            .unwrap();
        assert_eq!(first, 11442);

        // Second call: must NOT re-allocate. Allocator panics if invoked.
        let second = db
            .ensure_module_port(&pid, "vct-rl-reranker", || {
                panic!("allocator must NOT run on the second call")
            })
            .unwrap();
        assert_eq!(second, 11442);
    }

    /// Migration 014's backfill (in 017_module_ports.sql) populates the
    /// table from `projects.rl_port`. Verifies the round-trip via the
    /// `set_project_rl_port` legacy wrapper (covered separately, but we
    /// repeat the equivalence assertion here so the wrapper contract is
    /// pinned at the module_ports level too).
    #[test]
    fn rl_module_id_round_trips_via_generic_helpers() {
        let (db, pid) = open_db_with_project();
        db.set_module_port(&pid, "vct-rl-reranker", 11442).unwrap();
        // `get_project_rl_port` is a thin wrapper; if the refactor in
        // `db/projects.rs` ever drifts, this test fails loudly.
        assert_eq!(db.get_project_rl_port(&pid).unwrap(), Some(11442));
    }
}
