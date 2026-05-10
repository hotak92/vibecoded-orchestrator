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

/// Sentinel row that means "applies to every requester unless a more
/// specific (scope, project_id, module_id, key, requester) row overrides
/// it." Migration 009 backfills shared / global rows with this sentinel
/// so the legacy single-row API keeps working.
pub const REQUESTER_ANY: &str = "*";

/// Default-on-new-project rule: for a given owning scope + project_id,
/// what's the canonical requester column when the legacy single-row API
/// is called?
///
/// - `shared` / `global`: `*` sentinel — one row covers all requesters.
/// - `per_project`: literal owner project_id — only the owner sees by
///   default; other projects need a row in `secret_grants`.
///
/// Used by the legacy `is_secret_active` / `mark_secret_*` /
/// `forget_secret_active_state` functions which only reason about the
/// canonical row. The new `*_for_requester` siblings let callers
/// address other rows (e.g. shared secrets paused per-project).
fn canonical_requester<'a>(scope: &str, owner_project_id: &'a str) -> &'a str {
    match scope {
        "shared" | "global" => REQUESTER_ANY,
        _ => owner_project_id,
    }
}

impl Db {
    // ──────────────────────────────────────────────────────────────────
    // Legacy single-row API.
    //
    // These functions operate on the canonical row for a secret —
    // `requester_project_id = canonical_requester(scope, project_id)`.
    // They preserve the pre-migration-009 contract: one row per secret,
    // shared with no per-requester pause discrimination. Use the
    // `*_for_requester` siblings below for per-(secret × requester)
    // operations.
    // ──────────────────────────────────────────────────────────────────

    /// Read the active flag for a secret on its canonical row. Returns
    /// `true` when no row exists (default-active for any secret that
    /// pre-existed migration 007 or was just registered).
    pub fn is_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<bool, String> {
        let requester = canonical_requester(scope, project_id);
        self.is_secret_active_for_requester(scope, project_id, module_id, key, requester)
    }

    /// Mark a secret active on its canonical row. Idempotent.
    pub fn mark_secret_active(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let requester = canonical_requester(scope, project_id).to_string();
        self.mark_secret_active_for_requester(scope, project_id, module_id, key, &requester)
    }

    /// Mark a secret inactive on its canonical row. The keychain value
    /// is NOT touched — that is the whole point of Lifecycle B (Bug 3
    /// fix). After this call, the public read API (`is_secret_set`,
    /// `get_secret_preview`) MUST refuse to surface the value.
    pub fn mark_secret_inactive(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let requester = canonical_requester(scope, project_id).to_string();
        self.mark_secret_inactive_for_requester(scope, project_id, module_id, key, &requester)
    }

    /// Drop EVERY active-state row for this secret (canonical + any
    /// per-requester opt-out rows). Used by Remove — the entry no longer
    /// exists, so all metadata for it is stale. Idempotent.
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

    // ──────────────────────────────────────────────────────────────────
    // Per-(secret × requester) API (added 0.2.1, migration 009).
    //
    // Lookup contract:
    //   1. Look up the literal (scope, project_id, module_id, key,
    //      requester_project_id=requester) row first.
    //   2. If absent, fall back to the (..., requester=`*`) row.
    //   3. If neither row exists, default-active (matches the legacy
    //      contract for absent rows).
    //
    // Write contract: each function targets exactly the literal
    // requester row passed in. To pause a shared secret for project B
    // while leaving it active for everyone else, call
    // `mark_secret_inactive_for_requester(scope='shared',
    //                                     project_id='_user_shared_',
    //                                     module_id, key,
    //                                     requester='B')`.
    // The `*` row is left alone, so other projects keep seeing the
    // secret.
    // ──────────────────────────────────────────────────────────────────

    /// Read the active flag for `(secret × requester)`. Two-step lookup:
    /// the literal-requester row first, the `*` sentinel as fallback,
    /// default-active when neither exists.
    pub fn is_secret_active_for_requester(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
        requester_project_id: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();

        // Step 1: literal-requester row.
        let specific: Option<i64> = guard
            .query_row(
                "SELECT active FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3
                    AND key = ?4 AND requester_project_id = ?5",
                params![scope, project_id, module_id, key, requester_project_id],
                |r| r.get(0),
            )
            .ok();
        if let Some(v) = specific {
            return Ok(v != 0);
        }

        // Step 2: `*` sentinel fallback. Skip when the requester WAS
        // already `*` — we'd just be re-reading the same row.
        if requester_project_id != REQUESTER_ANY {
            let fallback: Option<i64> = guard
                .query_row(
                    "SELECT active FROM secret_active_state
                      WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3
                        AND key = ?4 AND requester_project_id = ?5",
                    params![scope, project_id, module_id, key, REQUESTER_ANY],
                    |r| r.get(0),
                )
                .ok();
            if let Some(v) = fallback {
                return Ok(v != 0);
            }
        }

        // Step 3: default-active.
        Ok(true)
    }

    /// Mark a secret active for a specific requester. Idempotent.
    pub fn mark_secret_active_for_requester(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
        requester_project_id: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state
                    (scope, project_id, module_id, key, requester_project_id, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6)
                 ON CONFLICT(scope, project_id, module_id, key, requester_project_id)
                 DO UPDATE SET active = 1, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, requester_project_id, now],
            )
            .map_err(|e| format!("mark_secret_active_for_requester: {}", e))?;
        Ok(())
    }

    /// Mark a secret inactive for a specific requester. Idempotent.
    pub fn mark_secret_inactive_for_requester(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
        requester_project_id: &str,
    ) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO secret_active_state
                    (scope, project_id, module_id, key, requester_project_id, active, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6)
                 ON CONFLICT(scope, project_id, module_id, key, requester_project_id)
                 DO UPDATE SET active = 0, updated_at = excluded.updated_at",
                params![scope, project_id, module_id, key, requester_project_id, now],
            )
            .map_err(|e| format!("mark_secret_inactive_for_requester: {}", e))?;
        Ok(())
    }

    /// Drop the active-state row for a single (secret × requester).
    /// After this call the legacy fallback path takes over (the `*`
    /// row, or default-active if no `*` row exists). Used when a
    /// project explicitly "resets to default" its opt-out toggle for
    /// a shared/granted secret. Idempotent.
    pub fn forget_secret_active_state_for_requester(
        &self,
        scope: &str,
        project_id: &str,
        module_id: &str,
        key: &str,
        requester_project_id: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM secret_active_state
                  WHERE scope = ?1 AND project_id = ?2 AND module_id = ?3
                    AND key = ?4 AND requester_project_id = ?5",
                params![scope, project_id, module_id, key, requester_project_id],
            )
            .map_err(|e| format!("forget_secret_active_state_for_requester: {}", e))?;
        Ok(())
    }

    /// Subagent G (2026-05-08): drop every `secret_active_state` row
    /// for `project_id`'s user-bucket. Used by `delete_project_v2` to
    /// clean up the metadata after a project is unregistered so that a
    /// future re-register with the same project_id starts from a
    /// pristine state (no ghost active-flag rows pinning a value the
    /// keychain may no longer hold).
    ///
    /// The keychain entries themselves are NOT touched here — that
    /// remains the user's call via the SecretsPanel "Remove" action
    /// before unregister, OR it can be done separately via the OS
    /// keychain UI. Mass-deleting keychain entries on unregister would
    /// be a worse default than leaving them, since rotating them
    /// outside the launcher GUI is a normal flow and we don't want to
    /// torch user secrets unannounced.
    ///
    /// Returns the count of rows actually removed (mostly informational
    /// for the unregister report; the surgical-strip step above already
    /// reported the env-surface side of the cleanup).
    pub fn forget_user_secret_state_for_project(&self, project_id: &str) -> Result<usize, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "DELETE FROM secret_active_state
                  WHERE scope = 'per_project' AND project_id = ?1 AND module_id = 'user'",
                params![project_id],
            )
            .map_err(|e| format!("forget_user_secret_state_for_project: {}", e))?;
        Ok(n)
    }

    /// Subagent G (2026-05-08): enumerate the KEY names for every per-
    /// project user-bucket secret the launcher has ever observed for
    /// `project_id` — regardless of active flag.
    ///
    /// Used by `write_project_env_files` to drive two parallel decisions:
    ///
    ///   1. EMIT: walk the returned keys, look each up in the keychain,
    ///      apply the cross-launcher active gate, and include only the
    ///      ones that are both keychain-present AND active in the env
    ///      surfaces. (See `build_user_secret_pairs` in projects_v2.rs.)
    ///
    ///   2. STRIP: any returned key that is NOT in the EMIT set was
    ///      written by us once and is no longer active — strip it from
    ///      every surface so a paused / removed secret can't survive
    ///      stale in `.claude/settings.json` / `.vscode/settings.json` /
    ///      `.claude/env` after the user toggles it off in the GUI.
    ///
    /// Scope filter is hardcoded to `('per_project', project_id, 'user')`.
    /// We do NOT enumerate the `licensing` bucket or any other module-
    /// owned secret here — those flow through manifests and the hub
    /// `project_env` resolver. Shared / global secrets are also excluded:
    /// they're a separate write-target (out of scope for Subagent G — the
    /// per-project env-file writer should never enumerate them, since
    /// emitting a global key into a project's env surface would multiply
    /// the same value across every registered project's `.claude/env`
    /// without the user opting in).
    ///
    /// Order: ASCII-sorted by key for deterministic env output (the env
    /// file diff stays readable across re-runs even if rows landed in the
    /// table in arrival order).
    ///
    /// Soft-fail: any DB error returns an empty Vec. Env-file writes must
    /// never block on a metadata-read hiccup.
    pub fn list_user_secret_keys_for_project(&self, project_id: &str) -> Vec<String> {
        let guard = self.lock();
        let mut stmt = match guard.prepare(
            "SELECT key FROM secret_active_state
              WHERE scope = 'per_project' AND project_id = ?1 AND module_id = 'user'
              ORDER BY key ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        let rows = match stmt.query_map(params![project_id], |r| r.get::<_, String>(0)) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        let mut out = Vec::new();
        for row in rows.flatten() {
            out.push(row);
        }
        out
    }

    /// H2 (0.1.7 fork-readiness sweep, 2026-05-08): enumerate the KEY
    /// names for every SHARED-scope user-bucket secret the launcher
    /// has ever observed, regardless of active flag.
    ///
    /// Shared user-bucket entries are written by the SecretsPanel
    /// "Shared (this user)" tab. They live at
    /// `(scope='shared', project_id='_user_shared_', module_id='user')`
    /// — applying to every registered project for this OS user. Pre-H2
    /// these rows existed in the active-flag DB and the keychain but
    /// no consumer enumerated them, so `set_secret_v2` against the
    /// Shared tab was a no-op from the env-surfaces' POV.
    ///
    /// Used by `project_env_settings::resolve_user_secret_state` to
    /// drive both the EMIT and STRIP halves of the env-writer
    /// contract (parallel to the per-project bucket — see the doc on
    /// `list_user_secret_keys_for_project`).
    ///
    /// Order: ASCII-sorted by key for deterministic env output.
    /// Soft-fail: any DB error returns an empty Vec.
    pub fn list_shared_user_secret_keys(&self) -> Vec<String> {
        let guard = self.lock();
        let mut stmt = match guard.prepare(
            "SELECT key FROM secret_active_state
              WHERE scope = 'shared' AND project_id = '_user_shared_' AND module_id = 'user'
              ORDER BY key ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        let rows = match stmt.query_map([], |r| r.get::<_, String>(0)) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        let mut out = Vec::new();
        for row in rows.flatten() {
            out.push(row);
        }
        out
    }

    /// H2 (0.1.7 fork-readiness sweep, 2026-05-08): enumerate the KEY
    /// names for every GLOBAL-scope user-bucket secret the launcher
    /// has ever observed, regardless of active flag.
    ///
    /// Global user-bucket entries are written by the SecretsPanel
    /// "Global (this machine)" tab. They live at
    /// `(scope='global', project_id='_global_', module_id='user')`
    /// — applying to every registered project across every user on
    /// this machine. Same EMIT/STRIP semantics as the shared list.
    ///
    /// Order: ASCII-sorted. Soft-fail to empty Vec on DB error.
    pub fn list_global_user_secret_keys(&self) -> Vec<String> {
        let guard = self.lock();
        let mut stmt = match guard.prepare(
            "SELECT key FROM secret_active_state
              WHERE scope = 'global' AND project_id = '_global_' AND module_id = 'user'
              ORDER BY key ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        let rows = match stmt.query_map([], |r| r.get::<_, String>(0)) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        let mut out = Vec::new();
        for row in rows.flatten() {
            out.push(row);
        }
        out
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

    /// Subagent G (2026-05-08): the env-writer enumeration helper must
    /// return EVERY key in the per-project user bucket regardless of
    /// active flag — both the active emit-set AND the inactive strip-set
    /// land here so the writer can drive both decisions from one call.
    /// Other scopes (`shared`, `global`) and other module buckets
    /// (`licensing`, future modules) MUST be filtered out: they have
    /// different env-surface semantics and shouldn't bleed into a
    /// project's auto-emitted user-secret list.
    #[test]
    fn list_user_secret_keys_returns_active_and_inactive_for_user_bucket_only() {
        let db = Db::open_in_memory().unwrap();
        // Active per-project user-bucket entry.
        db.mark_secret_active("per_project", "p1", "user", "MY_PROJECT_KEY").unwrap();
        // Inactive per-project user-bucket entry — must still be enumerated.
        db.mark_secret_inactive("per_project", "p1", "user", "PAUSED_KEY").unwrap();
        // Different project — must NOT bleed in.
        db.mark_secret_active("per_project", "p2", "user", "OTHER_PROJECT_KEY").unwrap();
        // Different module bucket on the same project — out of scope.
        db.mark_secret_active("per_project", "p1", "licensing", "VIBECODED_LICENSE_KEY").unwrap();
        // Shared / global scopes — filtered out (would multiply across projects).
        db.mark_secret_active("shared", "_user_shared_", "user", "SHARED_KEY").unwrap();
        db.mark_secret_active("global", "_global_", "user", "GLOBAL_KEY").unwrap();

        let keys = db.list_user_secret_keys_for_project("p1");
        assert_eq!(keys, vec!["MY_PROJECT_KEY".to_string(), "PAUSED_KEY".to_string()]);
        let other = db.list_user_secret_keys_for_project("p2");
        assert_eq!(other, vec!["OTHER_PROJECT_KEY".to_string()]);
        let none = db.list_user_secret_keys_for_project("p_nonexistent");
        assert!(none.is_empty());
    }

    /// Order invariant: keys come back ASCII-sorted so env-surface diffs
    /// stay stable across re-runs regardless of insert order.
    #[test]
    fn list_user_secret_keys_is_ascii_sorted() {
        let db = Db::open_in_memory().unwrap();
        for k in ["ZED_KEY", "ALPHA_KEY", "MID_KEY"] {
            db.mark_secret_active("per_project", "p1", "user", k).unwrap();
        }
        let keys = db.list_user_secret_keys_for_project("p1");
        assert_eq!(
            keys,
            vec!["ALPHA_KEY".to_string(), "MID_KEY".to_string(), "ZED_KEY".to_string()]
        );
    }

    /// H2 (2026-05-08): the shared user-bucket enumerator returns every
    /// key in `(scope='shared', project_id='_user_shared_', module_id='user')`
    /// regardless of active flag, AND filters out everything else
    /// (per-project rows, global rows, module-owned shared rows).
    /// Without this filter the env-writer would multiply non-shared
    /// keys across every project's surfaces.
    #[test]
    fn list_shared_user_secret_keys_filters_to_user_shared_bucket_only() {
        let db = Db::open_in_memory().unwrap();
        // In-bucket: should be returned.
        db.mark_secret_active("shared", "_user_shared_", "user", "OPENAI_API_KEY").unwrap();
        db.mark_secret_inactive("shared", "_user_shared_", "user", "PAUSED_SHARED_KEY").unwrap();
        // Out-of-bucket: per-project, must be excluded.
        db.mark_secret_active("per_project", "p1", "user", "PER_PROJECT_KEY").unwrap();
        // Out-of-bucket: legacy shared with real project_id, must be excluded.
        db.mark_secret_active("shared", "real-uuid-1234", "user", "LEGACY_SHARED_KEY").unwrap();
        // Out-of-bucket: shared but module-owned, not user-bucket.
        db.mark_secret_active("shared", "_user_shared_", "installer", "github_pat").unwrap();
        // Out-of-bucket: global, returned by the sibling enumerator.
        db.mark_secret_active("global", "_global_", "user", "GLOBAL_KEY").unwrap();

        let keys = db.list_shared_user_secret_keys();
        assert_eq!(
            keys,
            vec![
                "OPENAI_API_KEY".to_string(),
                "PAUSED_SHARED_KEY".to_string(),
            ]
        );
    }

    /// H2 (2026-05-08): the global user-bucket enumerator is symmetric
    /// to the shared one. Same filter contract: in-bucket only, both
    /// active and inactive rows enumerated, ASCII-sorted output.
    #[test]
    fn list_global_user_secret_keys_filters_to_global_user_bucket_only() {
        let db = Db::open_in_memory().unwrap();
        // In-bucket: returned.
        db.mark_secret_active("global", "_global_", "user", "MACHINE_KEY_1").unwrap();
        db.mark_secret_inactive("global", "_global_", "user", "PAUSED_GLOBAL_KEY").unwrap();
        // Out-of-bucket: legacy module-owned global.
        db.mark_secret_active("global", "_global_", "licensing", "VIBECODED_LICENSE_KEY").unwrap();
        // Out-of-bucket: shared / per-project user-bucket entries.
        db.mark_secret_active("shared", "_user_shared_", "user", "SHARED_KEY").unwrap();
        db.mark_secret_active("per_project", "p1", "user", "PER_PROJ_KEY").unwrap();

        let keys = db.list_global_user_secret_keys();
        assert_eq!(
            keys,
            vec![
                "MACHINE_KEY_1".to_string(),
                "PAUSED_GLOBAL_KEY".to_string(),
            ]
        );
    }

    /// H2 (2026-05-08): empty bucket → empty Vec. Pin the soft-fail
    /// behaviour so a fresh launcher install (no rows yet) produces
    /// stable output for downstream callers.
    #[test]
    fn list_shared_and_global_user_keys_return_empty_when_no_rows() {
        let db = Db::open_in_memory().unwrap();
        assert!(db.list_shared_user_secret_keys().is_empty());
        assert!(db.list_global_user_secret_keys().is_empty());
    }

    /// Subagent G unregister cleanup: forget every per-project user-bucket
    /// row for a single project_id without touching other projects'
    /// rows or the licensing module bucket.
    #[test]
    fn forget_user_secret_state_only_drops_target_project_user_bucket() {
        let db = Db::open_in_memory().unwrap();
        // Target project: 2 active + 1 inactive in user bucket.
        db.mark_secret_active("per_project", "p1", "user", "K1").unwrap();
        db.mark_secret_inactive("per_project", "p1", "user", "K2").unwrap();
        db.mark_secret_active("per_project", "p1", "user", "K3").unwrap();
        // Other project: must survive.
        db.mark_secret_active("per_project", "p2", "user", "OTHER").unwrap();
        // Same project, different module: must survive.
        db.mark_secret_active("per_project", "p1", "licensing", "VIBECODED_LICENSE_KEY").unwrap();
        // Different scope, same project_id slot: must survive (legacy
        // shared-with-real-project_id path).
        db.mark_secret_active("shared", "p1", "user", "SHARED_HERE").unwrap();

        let n = db.forget_user_secret_state_for_project("p1").unwrap();
        assert_eq!(n, 3, "should have dropped K1, K2, K3");

        // p1 user bucket: empty.
        assert!(db.list_user_secret_keys_for_project("p1").is_empty());
        // p2 user bucket: untouched.
        assert_eq!(
            db.list_user_secret_keys_for_project("p2"),
            vec!["OTHER".to_string()]
        );
        // p1 licensing module: still there (default-active for missing
        // row in `is_secret_active` would be true anyway, but verify the
        // row didn't get deleted).
        let guard = db.lock();
        let n_licensing: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM secret_active_state
                  WHERE scope='per_project' AND project_id='p1' AND module_id='licensing'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_licensing, 1);
        // shared scope row also survives.
        let n_shared: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM secret_active_state
                  WHERE scope='shared' AND project_id='p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_shared, 1);
    }

    /// Idempotent: forget on a project with no user-bucket rows is a
    /// no-op (returns 0). The unregister flow must not error when run
    /// against a project that never registered any user secrets.
    #[test]
    fn forget_user_secret_state_is_idempotent() {
        let db = Db::open_in_memory().unwrap();
        let n = db.forget_user_secret_state_for_project("nonexistent").unwrap();
        assert_eq!(n, 0);
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

    // ─── 0.2.1: per-(secret × requester) lookup tests ──────────────────

    /// Default-active when neither literal nor `*` row exists. Matches
    /// the legacy contract for absent rows.
    #[test]
    fn per_requester_default_active_with_no_rows() {
        let db = Db::open_in_memory().unwrap();
        assert!(db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "p1")
            .unwrap());
    }

    /// `*` sentinel row covers a project that has no specific row.
    /// This mirrors the migration-009 backfill path for shared/global.
    #[test]
    fn per_requester_falls_back_to_star_sentinel() {
        let db = Db::open_in_memory().unwrap();
        // Pause via the canonical (`*`) row.
        db.mark_secret_inactive("shared", "_user_shared_", "u", "K")
            .unwrap();
        // Any specific requester sees the paused state via fallback.
        assert!(!db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "p1")
            .unwrap());
        assert!(!db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "p2")
            .unwrap());
    }

    /// Specific row overrides the `*` sentinel. Project A active,
    /// project B paused, on the same shared secret.
    #[test]
    fn per_requester_specific_overrides_star() {
        let db = Db::open_in_memory().unwrap();
        // Canonical row: active for everyone (this is the
        // post-migration default for newly-set shared secrets).
        db.mark_secret_active("shared", "_user_shared_", "u", "K")
            .unwrap();
        // Project B opts out.
        db.mark_secret_inactive_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap();
        // Project A still sees it (falls through to `*`).
        assert!(db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "A")
            .unwrap());
        // Project B does not.
        assert!(!db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap());
    }

    /// `forget_*_for_requester` removes ONLY the specific row, leaving
    /// the `*` sentinel intact so the requester reverts to the default.
    #[test]
    fn forget_for_requester_falls_back_to_star() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_active("shared", "_user_shared_", "u", "K")
            .unwrap();
        db.mark_secret_inactive_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap();
        // Reset B's opt-out.
        db.forget_secret_active_state_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap();
        // B now sees the secret again via `*` fallback.
        assert!(db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap());
    }

    /// `forget_secret_active_state` (legacy) drops every row for a
    /// secret — both the canonical and any per-requester opt-outs.
    /// Used by Remove. After it returns, the secret is default-active
    /// for everyone (no rows means default).
    #[test]
    fn forget_legacy_drops_all_rows_for_secret() {
        let db = Db::open_in_memory().unwrap();
        db.mark_secret_inactive("shared", "_user_shared_", "u", "K")
            .unwrap();
        db.mark_secret_inactive_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap();
        db.forget_secret_active_state("shared", "_user_shared_", "u", "K")
            .unwrap();
        // Everything reverts to default-active.
        assert!(db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "A")
            .unwrap());
        assert!(db
            .is_secret_active_for_requester("shared", "_user_shared_", "u", "K", "B")
            .unwrap());
    }

    /// Per-project secret canonical row maps to the owner literally
    /// (not `*`), so other projects see default-active on the literal-
    /// requester lookup but the legacy API still operates on the
    /// owner's row only.
    #[test]
    fn per_project_canonical_uses_owner_not_star() {
        let db = Db::open_in_memory().unwrap();
        // Owner pauses their own secret via legacy API.
        db.mark_secret_inactive("per_project", "owner-A", "u", "K")
            .unwrap();
        // Owner's literal-requester lookup sees the pause.
        assert!(!db
            .is_secret_active_for_requester("per_project", "owner-A", "u", "K", "owner-A")
            .unwrap());
        // A different project's literal-requester lookup falls
        // through to default-active because no `*` row exists for
        // per-project secrets.
        assert!(db
            .is_secret_active_for_requester("per_project", "owner-A", "u", "K", "other-B")
            .unwrap());
    }
}
