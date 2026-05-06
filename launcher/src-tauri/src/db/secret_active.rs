//! Per-secret active flag — Storage A in the Bug 3 follow-up to PR #60.
//!
//! Secrets live in the OS keychain (see `crate::secrets`). This DB table
//! holds whether a registered secret is currently ACTIVE (readers may
//! receive its value) or INACTIVE (the value stays in the keychain so a
//! later one-click reactivation works without re-entry, but readers are
//! gated as if the secret were not set).
//!
//! Read-time gate is enforced in `commands/secrets_cmd.rs::is_secret_set`
//! and `get_secret_preview`. Both consult `is_active(...)` BEFORE
//! returning data — never trust the keychain alone.
//!
//! Default semantics: a secret with NO row in this table is ACTIVE. The
//! first `set_secret_v2` for a key writes `active=1` explicitly. `Unset`
//! writes `active=0`. `Reactivate` writes `active=1`. `Remove` deletes
//! the row.

use rusqlite::{params, Connection};

use super::Db;

impl Db {
    /// Read the active flag for a (scope, project_id, module_id, key)
    /// tuple. Returns `true` when no row exists (default-active for any
    /// secret that pre-existed migration 007 or was just registered).
    pub fn is_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let row: Option<i64> = guard
            .query_row(
                "SELECT active FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3 AND key = ?4",
                params![scope, project_id, module_id, key],
                |r| r.get(0),
            )
            .ok();
        Ok(row.map(|v| v != 0).unwrap_or(true))
    }

    /// Mark a secret active. Idempotent — safe to call from `set` and
    /// `reactivate` paths alike. Stamps `updated_at` to now.
    pub fn mark_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state (scope, project_id, module_id, key, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 1, ?5)
                 ON CONFLICT(scope, project_id, module_id, key)
                 DO UPDATE SET active = 1, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, now],
            )
            .map_err(|e| format!("mark_secret_active: {}", e))?;
        Ok(())
    }

    /// Mark a secret inactive. The keychain value is NOT touched — that is
    /// the whole point of Lifecycle B (Bug 3 fix). After this call, the
    /// public read API (`is_secret_set`, `get_secret_preview`) MUST refuse
    /// to surface the value.
    pub fn mark_secret_inactive(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state (scope, project_id, module_id, key, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 0, ?5)
                 ON CONFLICT(scope, project_id, module_id, key)
                 DO UPDATE SET active = 0, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, now],
            )
            .map_err(|e| format!("mark_secret_inactive: {}", e))?;
        Ok(())
    }

    /// Drop the active-state row entirely. Used by Remove (the entry no
    /// longer exists, so any "active" metadata for it is stale). Idempotent.
    pub fn forget_secret_active_state(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3 AND key = ?4",
                params![scope, project_id, module_id, key],
            )
            .map_err(|e| format!("forget_secret_active_state: {}", e))?;
        Ok(())
    }
}

// ─── Cross-launcher active-state read (PR-3 Commit 4, 2026-05-06) ─────
//
// Bug #5 follow-up — VCT_STATE_DIR isolates `launcher.db` per binary
// (~/.vct/ vs ~/.vct-dev/) but the OS keychain is SHARED across both
// (the keychain service-name prefix is the literal string "vct" — see
// secrets.rs:14 — and the Tauri bundle identifier is the same too).
//
// User-selected resolution (Option γ): keychain values stay shared, BUT
// the hub's `project_env` resolver must check active-state from EVERY
// known launcher's DB. If ANY launcher has explicitly paused the secret,
// the resolver treats it as paused — closing the gap where an unset in
// dev still leaked through prod's hub (and vice versa).
//
// Discovery: walks sibling dirs of the active launcher's state root for
// any `*launcher.db` file. Today that catches `~/.vct/` and `~/.vct-dev/`
// (the only two binaries we ship); a future third launcher binary that
// follows the same `~/.vct-<suffix>/` pattern is picked up automatically.
//
// Soft-fail: an unreadable / non-SQLite / locked sibling DB falls back
// to "no opinion" — i.e. doesn't make the secret look paused. The
// active-flag default ("no row" → active=true) means a launcher that
// has never seen the secret is silent on the question.

use std::path::{Path, PathBuf};

/// Walk the user's home dir for `~/.vct*` siblings. Returns absolute
/// paths to every `launcher.db` found, EXCLUDING the launcher's own
/// state-root (the caller has its own connection for that).
///
/// Soft-fail: returns an empty Vec on home-dir resolution errors,
/// directory read errors, etc.
pub fn discover_other_launcher_dbs(own_root: &Path) -> Vec<PathBuf> {
    let Some(home) = directories::UserDirs::new()
        .map(|d| d.home_dir().to_path_buf())
    else {
        return Vec::new();
    };
    discover_other_launcher_dbs_in(&home, own_root)
}

/// Implementation detail of `discover_other_launcher_dbs`: walks an
/// arbitrary search root. Factored out so unit tests can target a
/// scratch directory rather than the user's real `~/`.
pub fn discover_other_launcher_dbs_in(search_root: &Path, own_root: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(search_root) else {
        return out;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        // Match the `.vct*` naming convention used by both prod and dev
        // (~/.vct, ~/.vct-dev, future ~/.vct-anything).
        let name = match path.file_name().and_then(|s| s.to_str()) {
            Some(n) if n.starts_with(".vct") => n.to_string(),
            _ => continue,
        };
        // Skip the canonical secrets dir (`~/.vct-secrets/`) — it has no
        // launcher.db and the directory walk would needlessly inspect it.
        if name == ".vct-secrets" {
            continue;
        }
        // Skip the active launcher's own root — caller already queries it.
        if path == own_root {
            continue;
        }
        let candidate = path.join("launcher.db");
        if candidate.is_file() {
            out.push(candidate);
        }
    }
    out
}

/// Read `is_secret_active` from a sibling launcher DB at `db_path`.
/// Returns `None` if the DB can't be opened / the table is missing /
/// the read fails — caller should treat None as "no opinion" (don't
/// flip the resolver decision based on a sibling we can't read).
///
/// Returns `Some(true)` if the row says active OR no row exists (the
/// default-active semantic), `Some(false)` only when the sibling has an
/// explicit `active=0` row.
pub fn read_is_active_from_db_file(
    db_path: &Path,
    scope: &str,
    project_id: &str,
    module_id: &str,
    key: &str,
) -> Option<bool> {
    let conn = match Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(c) => c,
        Err(_) => return None,
    };
    // Defensive: the sibling DB might be from a launcher built before
    // migration 007 (table absent). Falling back to None rather than
    // erroring lets older launchers coexist with newer ones.
    let row: rusqlite::Result<i64> = conn.query_row(
        "SELECT active FROM secret_active_state
          WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3 AND key = ?4",
        params![scope, project_id, module_id, key],
        |r| r.get(0),
    );
    match row {
        Ok(v) => Some(v != 0),
        Err(rusqlite::Error::QueryReturnedNoRows) => Some(true), // default-active
        Err(_) => None,
    }
}

/// Cross-launcher active-state read used by the hub's secret resolver.
///
/// Decision rule (Option γ):
///   * Active iff EVERY known launcher (the caller's DB + every
///     discovered sibling) reports active.
///   * If ANY launcher reports inactive → return false.
///   * Sibling DBs we can't open / read are skipped (treated as "no
///     opinion") so a transient lock or migration mismatch can't
///     accidentally pause a secret.
///
/// Same defaults as `is_secret_active`: a (scope, project_id, module_id, key)
/// with no row in any DB resolves to TRUE.
pub fn is_secret_active_cross_launcher(
    own_db: &Db,
    scope: &str,
    project_id: &str,
    module_id: &str,
    key: &str,
) -> bool {
    // Own DB first — most common case is "no other launchers exist",
    // and we can short-circuit on a paused entry without paying the
    // sibling-discovery cost.
    let own_active = own_db
        .is_secret_active(scope, project_id, module_id, key)
        .unwrap_or(true);
    if !own_active {
        return false;
    }

    // Walk siblings.
    let own_root = crate::paths::vct_root_dir();
    for sibling in discover_other_launcher_dbs(&own_root) {
        match read_is_active_from_db_file(&sibling, scope, project_id, module_id, key) {
            Some(false) => {
                // Explicit pause anywhere → treat as paused.
                return false;
            }
            Some(true) | None => {
                // Active OR no opinion — keep checking.
                continue;
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_active_when_no_row() {
        let db = Db::open_in_memory().unwrap();
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }

    #[test]
    fn mark_inactive_then_reactivate_roundtrip() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("global", "_global_", "u", "K").unwrap();
        assert!(!db.is_secret_active("global", "_global_", "u", "K").unwrap());
        db.mark_secret_active("global", "_global_", "u", "K").unwrap();
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }

    #[test]
    fn forget_resets_to_default() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("global", "_global_", "u", "K").unwrap();
        db.forget_secret_active_state("global", "_global_", "u", "K").unwrap();
        // After forget, default-active applies again.
        assert!(db.is_secret_active("global", "_global_", "u", "K").unwrap());
    }

    // ─── PR-3 Commit 4: cross-launcher reads (Option γ) ─────────────────

    use rusqlite::Connection;
    use std::sync::Mutex as StdMutex;

    /// Build a fresh on-disk DB at `path` with the `secret_active_state`
    /// table populated so `read_is_active_from_db_file` has something
    /// real to read. Returns a `Db` handle for cleanup.
    fn make_db_at_path(path: &Path) -> Db {
        let conn = Connection::open(path).unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(StdMutex::new(conn))
    }

    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-cross-launcher-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn read_is_active_from_db_file_returns_default_when_no_row() {
        let dir = scratch_dir("read-default");
        let db_path = dir.join("launcher.db");
        let _db = make_db_at_path(&db_path);

        let active =
            read_is_active_from_db_file(&db_path, "global", "_global_", "u", "K");
        assert_eq!(active, Some(true), "no row should default to active");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_is_active_from_db_file_returns_explicit_inactive() {
        let dir = scratch_dir("read-inactive");
        let db_path = dir.join("launcher.db");
        let db = make_db_at_path(&db_path);

        db.mark_secret_inactive("global", "_global_", "u", "K").unwrap();

        // Drop the in-process handle so the cross-launcher reader can
        // open the same file in read-only mode without lock contention.
        drop(db);

        let active =
            read_is_active_from_db_file(&db_path, "global", "_global_", "u", "K");
        assert_eq!(active, Some(false));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_is_active_from_db_file_returns_none_for_missing_db() {
        let bogus = std::path::PathBuf::from("/tmp/nonexistent-cross-launcher-test.db");
        let active = read_is_active_from_db_file(&bogus, "global", "_global_", "u", "K");
        assert_eq!(active, None, "missing DB should yield 'no opinion'");
    }

    #[test]
    fn cross_launcher_returns_active_when_no_siblings_exist() {
        // VCT_STATE_DIR isolation: if no siblings of the launcher's
        // root exist, cross-launcher reads degrade to single-DB
        // behaviour. Defaults match `is_secret_active`.
        let db = Db::open_in_memory().unwrap();
        // We can't easily prevent the homedir from having ~/.vct or ~/.vct-dev
        // here. Instead, isolate the test by setting VCT_STATE_DIR so the
        // own_root resolver picks up a temp dir; if a real ~/.vct exists,
        // it'll legitimately count as a sibling but its row for our random
        // (project, module, key) tuple will be absent → "active".
        assert!(is_secret_active_cross_launcher(
            &db,
            "global",
            "_global_",
            "user",
            "PR3_CL_TEST_NO_SIBLINGS",
        ));
    }

    #[test]
    fn cross_launcher_blocks_when_own_db_says_inactive() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("global", "_global_", "u", "PR3_CL_TEST_OWN_INACTIVE")
            .unwrap();
        assert!(!is_secret_active_cross_launcher(
            &db,
            "global",
            "_global_",
            "u",
            "PR3_CL_TEST_OWN_INACTIVE",
        ));
    }

    #[test]
    fn discover_finds_sibling_launcher_dbs_excludes_own_root() {
        let search_root = scratch_dir("discover");
        // Build three fake VCT state dirs as siblings of each other.
        let prod = search_root.join(".vct");
        let dev = search_root.join(".vct-dev");
        let third = search_root.join(".vct-experimental");
        for d in [&prod, &dev, &third] {
            std::fs::create_dir_all(d).unwrap();
            // Drop a placeholder launcher.db so the discovery picks it up.
            std::fs::File::create(d.join("launcher.db")).unwrap();
        }
        // Add a non-vct sibling — must be ignored.
        std::fs::create_dir_all(search_root.join("Documents")).unwrap();
        // Add the secrets dir — must be ignored even though it starts with .vct.
        std::fs::create_dir_all(search_root.join(".vct-secrets")).unwrap();

        let found = discover_other_launcher_dbs_in(&search_root, &prod);
        // Should find dev + third, NOT prod (own) and NOT .vct-secrets.
        assert_eq!(found.len(), 2);
        let names: Vec<String> = found
            .iter()
            .map(|p| p.parent().unwrap().file_name().unwrap().to_string_lossy().to_string())
            .collect();
        assert!(names.contains(&".vct-dev".to_string()));
        assert!(names.contains(&".vct-experimental".to_string()));
        assert!(!names.contains(&".vct".to_string()));
        assert!(!names.contains(&".vct-secrets".to_string()));

        std::fs::remove_dir_all(&search_root).ok();
    }

    /// Full integration: build two on-disk launcher DBs (a "prod" + a
    /// "dev"), put the prod one as the caller's "own", pause a secret
    /// in the dev one, and verify
    /// `read_is_active_from_db_file(dev_path)` reports the pause.
    /// Combined with `cross_launcher_blocks_when_own_db_says_inactive`
    /// (above), this proves the Option γ wiring: any launcher's pause
    /// blocks the resolver.
    #[test]
    fn sibling_launcher_pause_propagates_via_read_helper() {
        let dir = scratch_dir("propagate");
        let dev_path = dir.join(".vct-dev").join("launcher.db");
        std::fs::create_dir_all(dev_path.parent().unwrap()).unwrap();
        let dev_db = make_db_at_path(&dev_path);

        // Pause in dev.
        dev_db
            .mark_secret_inactive("global", "_global_", "u", "PR3_CL_PROPAGATE")
            .unwrap();
        // Drop the in-process handle so the cross-launcher reader can
        // open the same file in read-only mode.
        drop(dev_db);

        // Sibling discovery + read.
        let active = read_is_active_from_db_file(
            &dev_path,
            "global",
            "_global_",
            "u",
            "PR3_CL_PROPAGATE",
        );
        assert_eq!(
            active,
            Some(false),
            "dev launcher's pause must be readable from prod side"
        );

        std::fs::remove_dir_all(&dir).ok();
    }
}
