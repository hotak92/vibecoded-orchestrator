//! SQLite-backed persistence for projects, modules, settings, and access grants.
//!
//! All state that outlives a single launcher session lives here (apart from
//! secrets, which go to the OS keychain — see `crate::secrets`).
//!
//! The DB lives at `<VCT_STATE_DIR or ~/.vct>/launcher.db` (Bug 14:
//! VCT_STATE_DIR overrides for dev/prod isolation; see `crate::paths::vct_root_dir`).
//! It is opened in WAL mode so reads don't block writers and so crashed
//! writes don't corrupt the file.
//!
//! We use synchronous `rusqlite` calls wrapped in `tokio::task::spawn_blocking`
//! at the call sites in `commands/*.rs`. Each command captures the `DbPool`
//! handle from Tauri state and runs short transactions. Long-running work
//! (installs, downloads) never holds the DB lock.

use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

use rusqlite::Connection;

pub mod migrations;
pub mod models;
pub mod projects;
pub mod modules;
pub mod settings;
pub mod access;
pub mod tier;
pub mod project_state;
pub mod project_mcp_servers;
pub mod slug;
pub mod change_log;
pub mod code_graph_builds;
pub mod kg_syncs;
pub mod kg_summaries;
// v0.2.54 Track J — shared log-tail truncation (LOG_TAIL_MAX_BYTES +
// char-boundary-safe capping), extracted from SIX per-module copies in
// the three db writers above + the launcher crate's command layer.
pub mod log_tail;
pub mod secret_active;
pub mod secret_grants;
pub mod app_state;
pub mod audit_types;
pub mod orchestrator_root_helpers;
// module_weights_state removed in v0.2.31 (Agent J): table dropped by
// migration 020; weights state is now container-owned in rl_weights_state
// (shipped by vct-rl-reranker v0.2.6 via its module-shipped migration
// `db/0002_*.sql`). Launcher reads go through the hub's
// `/api/v1/modules/.../rows/rl_weights_state/...` endpoint.
pub mod module_ports;
pub mod deprecation_events;
pub mod module_db_migrations;
// Migration 021 — diagrams (Mermaid + Excalidraw) registry, snapshots,
// access grants, per-tool MCP grants, per-project module-active flags.
// Phase 1.1 of the diagrams integration plan
// (.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md).
pub mod diagrams;
// Migration 023 — module-shipped MCP tool allowlist defaults
// (v0.2.34 Agent E — Phase 4 generalisation). Populated at install
// time from manifest.mcp_registration.tool_allowlist; read by the hub's
// /mcp-tool-grants route to compose per-project allowlists.
pub mod mcp_tool_defaults;
// Migration 024 — per-paid-module license keys (v0.2.40 L1). The raw
// key value stays in the OS keychain; this table only holds the SOURCE-
// of-input metadata (prefix for display, keychain coordinates, last
// validation outcome). Effective tier still projects through
// `tier_cache.module_licenses` — `tier.rs` stays unchanged.
pub mod license_keys;
// Migration 025 — RL telemetry events queryable store (v0.2.47). Replaces
// the JSONL corpus at `~/.claude/retrieval_rl_data/rl_events.jsonl`. The
// MCP-side telemetry writer POSTs every event via the hub's
// `POST /api/v1/rl/events` route; the hub is the sole writer (preserves
// the launcher's single-writer architectural rule).
pub mod rl_events;
// Migration 026 — per-project extra codegraph paths (v0.2.47). Read-only
// filesystem roots contributing entities to the project's codegraph
// collection. Tauri command surface in
// `commands::project_codegraph_extras` (launcher crate); hub resolver
// exposes enabled rows via the additive `code_graph_extra_paths` field.
// Plan: .claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md.
pub mod codegraph_extras;

/// Resolve the launcher DB path: `<VCT_STATE_DIR or ~/.vct>/launcher.db`.
pub fn db_path() -> PathBuf {
    crate::paths::vct_root_dir().join("launcher.db")
}

/// Apply the canonical connection pragmas. SINGLE source of truth so the
/// startup `open()` and the v0.2.60 `reopen_after_update()` can never drift
/// (a forgotten `busy_timeout` on reopen would silently change post-update
/// contention behaviour — see the launcher-self-db-lock fix).
fn apply_connection_pragmas(conn: &Connection) -> Result<(), String> {
    // WAL for concurrent readers + durable writes.
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| format!("enable WAL: {}", e))?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| format!("set synchronous: {}", e))?;
    conn.pragma_update(None, "foreign_keys", "ON")
        .map_err(|e| format!("enable FK: {}", e))?;
    // Match install.py's sqlite3.connect(..., timeout=5.0) so both sides
    // wait up to 5 s before raising SQLITE_BUSY rather than failing
    // immediately when the launcher and a Python install script contend.
    conn.pragma_update(None, "busy_timeout", 5000)
        .map_err(|e| format!("set busy_timeout: {}", e))?;
    Ok(())
}

/// Thread-safe connection handle stored in Tauri managed state.
///
/// A single `rusqlite::Connection` is NOT `Sync`, so we wrap it in a `Mutex`.
/// The DB operations here are short (single-digit ms) so lock contention is
/// not a concern at launcher scale. If it becomes one we'll switch to
/// `deadpool-sqlite` or similar.
pub struct Db(pub Mutex<Connection>);

impl Db {
    /// Open the DB, ensure parent dir exists, apply all pending migrations.
    pub fn open() -> Result<Self, String> {
        let path = db_path();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("create {}: {}", parent.display(), e))?;
        }

        let conn = Connection::open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;

        apply_connection_pragmas(&conn)?;

        migrations::apply(&conn)?;

        let db = Db(Mutex::new(conn));
        db.ensure_change_log()?;
        // Best-effort prune of old change_log rows. Silent failure is fine.
        let cutoff = chrono::Utc::now().timestamp_millis() - 24 * 60 * 60 * 1000;
        let _ = db.prune_change_log(cutoff);

        // NOTE (v0.2.21 Step 3d): the orchestrator-root auto-register
        // call previously ran from inside `Db::open()`, but its
        // implementation depends on launcher-only modules
        // (`commands::modules::find_orchestrator_manifest`,
        // `commands::projects_v2::sanitize_kg_collection`) that cannot
        // move into `vct-launcher-core` without dragging in Tauri-runtime
        // dependencies. Hoisted to launcher's `lib.rs::run` setup() block
        // (callers: launcher GUI on startup). The hub binary does not
        // need this call — orchestrator-root registration is a launcher
        // convenience surface, not a hub responsibility.

        Ok(db)
    }

    /// Borrow the connection guard for a short transaction.
    ///
    /// The caller receives a `MutexGuard<Connection>`. Drop it promptly —
    /// do NOT hold it across an `.await`. For async operations, collect
    /// needed data under the lock then release before awaiting.
    pub fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.0.lock().expect("db mutex poisoned")
    }

    /// v0.2.60: release the launcher's OS handle on `launcher.db` for the
    /// `install.py --update` window, so install.py can take the SQLite
    /// writer lock (Windows holds it exclusively — see the
    /// launcher-self-db-lock bug). The managed connection is swapped for a
    /// throwaway schema-less in-memory connection; any stray managed write
    /// during the window then fails LOUDLY (`no such table`) instead of
    /// silently persisting to a file we'd discard. Pollers that open their
    /// OWN connections are gated separately via
    /// `update_gate::skip_if_update_in_progress` — BOTH are required for
    /// the "zero launcher-side handles" guarantee.
    ///
    /// Poison-tolerant: recovers a poisoned mutex (`into_inner`) rather than
    /// panicking, so a prior panic can't wedge the update. The in-memory
    /// stand-in is built BEFORE the lock so an open failure is a clean
    /// `Err`, never a mid-swap panic. `wal_checkpoint(TRUNCATE)` is
    /// best-effort (flushes/shrinks the `-wal`); a partial checkpoint is
    /// fine since the pollers stand down.
    ///
    /// Pair with [`reopen_after_update`] (an RAII guard in the launcher
    /// calls it on every exit path).
    pub fn close_for_update(&self) -> Result<(), String> {
        // Build the stand-in FIRST (fallible) — outside the lock.
        let standin = Connection::open_in_memory()
            .map_err(|e| format!("close_for_update: open stand-in: {}", e))?;

        let mut guard = self.0.lock().unwrap_or_else(|p| p.into_inner());
        // Best-effort WAL flush + shrink so install.py sees a consistent
        // main DB and the -wal/-shm aren't left large/open.
        if let Err(e) = guard.pragma_update(None, "wal_checkpoint", "TRUNCATE") {
            eprintln!("[db] close_for_update: wal_checkpoint(TRUNCATE) best-effort failed: {}", e);
        }
        // Infallible move: swap the live conn out, stand-in in.
        let real = std::mem::replace(&mut *guard, standin);
        drop(guard); // release the mutex before the (possibly slow) close.

        // Explicitly close the real connection so the OS handle (+ -wal/-shm
        // on Windows) is released deterministically BEFORE we return — do
        // not rely on scope-end Drop timing.
        if let Err((conn, e)) = real.close() {
            eprintln!(
                "[db] close_for_update: Connection::close() returned err ({}); \
                 dropping handle explicitly",
                e
            );
            drop(conn);
        }
        Ok(())
    }

    /// v0.2.60: reopen the real `launcher.db` file connection after the
    /// update window, restoring the managed connection. Re-applies the
    /// SAME pragmas as `open()` (via the shared `apply_connection_pragmas`)
    /// and re-runs `migrations::apply` (idempotent — this re-establishes
    /// the open() invariant; it is NOT relied upon to pick up install.py
    /// migrations: install.py provably does not own/migrate launcher.db).
    ///
    /// Poison-tolerant. On failure the launcher MUST NOT continue running
    /// on the schema-less stand-in (that would silently discard writes) —
    /// the caller (RAII guard) treats a reopen error as fatal and forces a
    /// restart/exit.
    pub fn reopen_after_update(&self) -> Result<(), String> {
        let path = db_path();
        let conn = Connection::open(&path)
            .map_err(|e| format!("reopen_after_update: open {}: {}", path.display(), e))?;
        apply_connection_pragmas(&conn)?;
        migrations::apply(&conn)?;

        let mut guard = self.0.lock().unwrap_or_else(|p| p.into_inner());
        let standin = std::mem::replace(&mut *guard, conn);
        drop(guard);
        // The stand-in was in-memory; dropping it is cheap and infallible.
        drop(standin);
        Ok(())
    }

    /// Open an in-memory DB for tests. Runs all migrations + ensures the
    /// change_log table is present, mirroring the production `open()`
    /// path. Each call returns a fresh isolated DB.
    #[cfg(any(test, debug_assertions))]
    pub fn open_in_memory() -> Result<Self, String> {
        let conn = Connection::open_in_memory()
            .map_err(|e| format!("open in-memory: {}", e))?;
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| format!("enable FK: {}", e))?;
        migrations::apply(&conn)?;
        let db = Db(Mutex::new(conn));
        db.ensure_change_log()?;
        Ok(db)
    }
}

// ─── Audit actor (OS user) ───────────────────────────────────────────────
//
// The audit_log.actor column needs the username of whoever is running
// the launcher. Resolved once at process startup by reading $USER /
// $USERNAME, with a literal "unknown" fallback. Stored in a OnceLock so
// every call to `Db::audit(...)` can stamp it without re-reading env or
// taking on a `whoami` crate dependency.

static AUDIT_ACTOR: OnceLock<String> = OnceLock::new();

/// Resolve and cache the current OS user. Called once at process start;
/// subsequent calls return the cached value.
pub fn current_actor() -> &'static str {
    AUDIT_ACTOR.get_or_init(|| {
        std::env::var("USER")
            .or_else(|_| std::env::var("USERNAME"))
            .unwrap_or_else(|_| "unknown".to_string())
    })
}

#[cfg(test)]
mod close_reopen_tests {
    //! v0.2.60 — `close_for_update` / `reopen_after_update` (the
    //! launcher-self-db-lock fix). These exercise the REAL file open/close
    //! against `db_path()`, so they mutate the process-wide `VCT_STATE_DIR`
    //! and MUST be serialised (same pattern as `lockfile::tests`).
    use super::*;
    use std::sync::Mutex as StdMutex;

    static SERIALIZE: StdMutex<()> = StdMutex::new(());

    fn with_state_dir<F: FnOnce(&std::path::Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        // SAFETY: serialised by SERIALIZE; no other thread observes/mutates
        // VCT_STATE_DIR concurrently.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }
        f(tmp.path());
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    #[test]
    fn close_for_update_releases_file_handle_for_a_second_writer() {
        with_state_dir(|_root| {
            let db = Db::open().expect("open");
            // Seed a row so there's real content + a populated schema.
            db.app_state_set("v0260_probe", "before").expect("seed");

            // Close the managed connection for the "update window".
            db.close_for_update().expect("close_for_update");

            // A SECOND connection (mimicking install.py / a fresh-conn
            // poller) must now be able to open the file RW and WRITE —
            // proving the launcher released its OS handle.
            let path = db_path();
            let other = Connection::open(&path).expect("second open");
            other
                .pragma_update(None, "busy_timeout", 2000)
                .expect("busy_timeout");
            other
                .execute(
                    "INSERT OR REPLACE INTO app_state(key, value, updated_at) \
                     VALUES('v0260_external','x', strftime('%s','now')*1000)",
                    [],
                )
                .expect("external write must succeed while launcher conn is closed");
            drop(other);

            // Reopen and confirm the external write is visible + a managed
            // write works again.
            db.reopen_after_update().expect("reopen");
            let seen: String = db
                .lock()
                .query_row(
                    "SELECT value FROM app_state WHERE key='v0260_external'",
                    [],
                    |r| r.get(0),
                )
                .expect("read external write back");
            assert_eq!(seen, "x");
            db.app_state_set("v0260_probe", "after").expect("managed write after reopen");
        });
    }

    #[test]
    fn close_then_reopen_round_trips_and_is_repeatable() {
        with_state_dir(|_root| {
            let db = Db::open().expect("open");
            db.app_state_set("rt", "1").expect("seed");
            for _ in 0..3 {
                db.close_for_update().expect("close");
                db.reopen_after_update().expect("reopen");
            }
            // Schema + data still intact after repeated close/reopen.
            let v: String = db
                .lock()
                .query_row("SELECT value FROM app_state WHERE key='rt'", [], |r| {
                    r.get(0)
                })
                .expect("row survives close/reopen cycles");
            assert_eq!(v, "1");
        });
    }

    #[test]
    fn reopen_preserves_schema_version() {
        with_state_dir(|_root| {
            let db = Db::open().expect("open");
            let before: i64 = db
                .lock()
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .expect("user_version before");
            db.close_for_update().expect("close");
            db.reopen_after_update().expect("reopen");
            let after: i64 = db
                .lock()
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .expect("user_version after");
            assert_eq!(
                before, after,
                "reopen must not change the schema version (no surprise migration)"
            );
        });
    }

    #[test]
    fn close_for_update_recovers_a_poisoned_mutex() {
        with_state_dir(|_root| {
            let db = Db::open().expect("open");
            // Poison the mutex.
            let poisoned = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let _g = db.0.lock().unwrap();
                panic!("intentional poison");
            }));
            assert!(poisoned.is_err());
            // close/reopen must NOT panic on the poisoned mutex.
            db.close_for_update().expect("close tolerates poison");
            db.reopen_after_update().expect("reopen tolerates poison");
        });
    }

    #[test]
    fn reopen_after_update_restores_a_working_busy_timeout() {
        // N7: reopen must re-apply ALL pragmas. busy_timeout is the
        // load-bearing one for post-update contention; assert it's set.
        with_state_dir(|_root| {
            let db = Db::open().expect("open");
            db.close_for_update().expect("close");
            db.reopen_after_update().expect("reopen");
            let bt: i64 = db
                .lock()
                .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
                .expect("busy_timeout pragma");
            assert_eq!(bt, 5000, "busy_timeout must be re-applied on reopen");
        });
    }
}
