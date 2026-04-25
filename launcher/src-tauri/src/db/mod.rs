//! SQLite-backed persistence for projects, modules, settings, and access grants.
//!
//! All state that outlives a single launcher session lives here (apart from
//! secrets, which go to the OS keychain — see `crate::secrets`).
//!
//! The DB lives at `~/.vct/launcher.db`. It is opened in WAL mode so reads
//! don't block writers and so crashed writes don't corrupt the file.
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

/// Resolve the launcher DB path: `~/.vct/launcher.db`.
pub fn db_path() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("launcher.db"))
        .unwrap_or_else(|| PathBuf::from(".vct/launcher.db"))
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
                .map_err(|e| format!("create ~/.vct/: {}", e))?;
        }

        let conn = Connection::open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;

        // WAL for concurrent readers + durable writes.
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| format!("enable WAL: {}", e))?;
        conn.pragma_update(None, "synchronous", "NORMAL")
            .map_err(|e| format!("set synchronous: {}", e))?;
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| format!("enable FK: {}", e))?;

        migrations::apply(&conn)?;

        Ok(Db(Mutex::new(conn)))
    }

    /// Borrow the connection guard for a short transaction.
    ///
    /// The caller receives a `MutexGuard<Connection>`. Drop it promptly —
    /// do NOT hold it across an `.await`. For async operations, collect
    /// needed data under the lock then release before awaiting.
    pub fn lock(&self) -> std::sync::MutexGuard<Connection> {
        self.0.lock().expect("db mutex poisoned")
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
