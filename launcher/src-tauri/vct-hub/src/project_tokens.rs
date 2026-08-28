// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Per-project resolver tokens (v0.2.76 Part 4).
//!
//! ─── Why this exists ────────────────────────────────────────────────
//!
//! Before this, the ONLY credential the resolver clients
//! (`vct_project_config.*`, `vct_secrets_resolve.*`, `agent_secrets.py`,
//! `project_config.py`) could present to the two per-project routes —
//! `GET /api/v1/projects/{id}/env` and `GET /api/v1/projects/{id}/config`
//! — was the coarse hub-wide `hub.token`. Any local process that could
//! read `hub.token` could therefore read **every** project's resolved
//! env (secrets) and config, not just its own. `hub.token` is a single
//! blast radius for all projects on the machine.
//!
//! This module mints a **per-project** bearer token so a resolver that
//! knows its own project id (they all do — it's the lookup key) can
//! present a credential scoped to exactly that project. A token minted
//! for project A must NOT resolve project B (`auth.rs` returns a hard
//! 403 for that case). The hub-wide `hub.token` stays accepted on these
//! two routes for a one-release compatibility window (see
//! `VCT_HUB_LEGACY_GLOBAL_ENV` in `auth.rs`).
//!
//! ─── Lifecycle (least-machinery, see the Part-4 brief) ──────────────
//!
//! Tokens are minted at hub **startup** for every project row in
//! `launcher.db`, written to `<vct_root_dir>/hub.token.<project_id>`
//! (mode 0o600 on Unix — same discipline as `hub.token` itself), and
//! held in an in-memory registry the auth middleware consults per
//! request. On each startup:
//!
//!   * a fresh token is generated per project (SAME rotation lifecycle
//!     as `hub.token` — regenerated every startup so a leaked token's
//!     window is "from this start to the next restart");
//!   * stale `hub.token.<id>` files for projects that no longer exist in
//!     `launcher.db` are removed (so a deleted project's file doesn't
//!     linger with a live-looking token).
//!
//! The hub is a detached process with **no push signal** from the
//! launcher on project add/remove (it reads `launcher.db` per request,
//! but nothing tells it "a project appeared"). Rather than add a timer
//! or lazily mint-on-401 (a write-on-read side effect), we mint at
//! startup only. A project added *while the hub is already running* has
//! no per-project token file yet — its resolver simply falls back to the
//! hub-wide `hub.token` (the compat window keeps that working) until the
//! next hub restart re-reads the registry. This is the least machinery
//! that satisfies the contract: no new plumbing, no background task, no
//! write-on-read; it reuses the startup registry read the hub already
//! performs for the bind decision.
//!
//! ─── Threat model (same as `hub.token`) ─────────────────────────────
//!
//! The per-project token file is mode 0o600 — same-user-only. A
//! same-user attacker with arbitrary code execution can read every
//! `hub.token.<id>` just as they can read `hub.token` and the OS
//! keychain; this module does not raise the bar against that adversary.
//! What it DOES is shrink the blast radius of a token that leaks to a
//! *different* local user or process that can read one project's file
//! but not another's, and it gives resolvers a scoped credential so a
//! future release can turn OFF the coarse global-token path entirely
//! (the `VCT_HUB_LEGACY_GLOBAL_ENV=0` posture).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};

use vct_launcher_core::db::Db;
use vct_launcher_core::services::boot_token;

/// Filename prefix for per-project token files under `vct_root_dir()`.
/// A project's file is `hub.token.<project_id>`. Kept distinct from the
/// bare `hub.token` (global) so a glob of `hub.token.*` never matches
/// the global file.
pub const PROJECT_TOKEN_PREFIX: &str = "hub.token.";

/// In-memory registry of per-project resolver tokens, populated at hub
/// startup. Shared into the auth middleware as an axum extension.
///
/// Wrapped in `Arc<RwLock<..>>` so the middleware closure clones cheaply
/// (one atomic increment per request) AND a mid-session project add can
/// lazily mint its token into the LIVE registry without a hub restart
/// (v0.2.77 Part 8 Task 4a). Reads (the per-request reverse-lookup) take
/// the read lock; the rare lazy-mint takes the write lock. Before 4a the
/// map was an immutable `Arc<HashMap>` and a mid-session-added project had
/// to wait for the next startup — which stranded its resolver once the
/// global-token compat window closed (the flip). Startup re-mint is
/// unchanged: `mint_project_tokens` still rebuilds the whole map on every
/// boot, so lazy-minted tokens rotate exactly like startup-minted ones.
#[derive(Clone)]
pub struct ProjectTokenRegistry {
    /// project_id → token. Empty when no projects are registered.
    by_project: Arc<RwLock<HashMap<String, String>>>,
}

impl ProjectTokenRegistry {
    /// Build an empty registry (used when the DB read fails — the hub
    /// still boots; every resolver falls back to the global token).
    pub fn empty() -> Self {
        Self {
            by_project: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Construct directly from a project_id → token map. Primarily for
    /// tests; production uses [`mint_project_tokens`].
    pub fn from_map(map: HashMap<String, String>) -> Self {
        Self {
            by_project: Arc::new(RwLock::new(map)),
        }
    }

    /// Acquire the read guard, recovering a poisoned lock (a panic while
    /// the write lock was held must not permanently 500 every auth call —
    /// the map is plain data, so the poisoned contents are still valid).
    fn read_map(&self) -> std::sync::RwLockReadGuard<'_, HashMap<String, String>> {
        self.by_project.read().unwrap_or_else(|p| p.into_inner())
    }

    /// The token minted for `project_id`, if any. `None` when the
    /// project has no per-project token (not registered at startup, or
    /// added mid-session before its lazy-mint fired).
    ///
    /// Returns an owned `String` (not a borrow) because the value lives
    /// behind the `RwLock` — the guard cannot outlive this call.
    pub fn token_for(&self, project_id: &str) -> Option<String> {
        self.read_map().get(project_id).cloned()
    }

    /// Reverse lookup: which project (if any) does this bearer token
    /// authenticate as? Constant-time compare against every registered
    /// token so a caller can't learn "was this a valid token for SOME
    /// project?" via timing. Returns the owning project_id on a match.
    ///
    /// Used by the auth middleware to distinguish three cases on the
    /// per-project routes:
    ///   * bearer == token_for(url_project) → allow;
    ///   * bearer == token_for(OTHER_project) → hard 403 (valid token,
    ///     wrong project);
    ///   * bearer matches no project token → fall through to the global
    ///     token check (compat / lazy-mint) → 401 if that also fails.
    pub fn project_for_token(&self, bearer: &str) -> Option<String> {
        let map = self.read_map();
        let mut matched: Option<String> = None;
        for (pid, tok) in map.iter() {
            // Walk EVERY entry (no early break) so the number of
            // comparisons doesn't leak how far down the map a match sat.
            if boot_token::constant_time_eq(bearer.as_bytes(), tok.as_bytes()) {
                matched = Some(pid.clone());
            }
        }
        matched
    }

    /// Insert (or overwrite) a freshly-minted token for `project_id` into
    /// the LIVE registry. Used only by the lazy-mint path (4a); startup
    /// minting builds a fresh map wholesale via [`from_map`]. Idempotent
    /// from the caller's view — re-inserting the same id just replaces the
    /// value (the lazy-mint path only calls this when no entry existed, so
    /// in practice this is an insert, not an overwrite).
    pub fn insert(&self, project_id: String, token: String) {
        let mut map = self
            .by_project
            .write()
            .unwrap_or_else(|p| p.into_inner());
        map.insert(project_id, token);
    }

    /// Number of registered per-project tokens. Test/diagnostic aid.
    pub fn len(&self) -> usize {
        self.read_map().len()
    }

    /// Whether the registry holds no tokens.
    pub fn is_empty(&self) -> bool {
        self.read_map().is_empty()
    }
}

/// Path to a project's token file under the launcher's state-root.
pub fn project_token_path(project_id: &str) -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join(format!("{}{}", PROJECT_TOKEN_PREFIX, project_id))
}

/// Mint per-project resolver tokens for every project in `launcher.db`.
///
/// Called once from `server::start_hub_server` AFTER the global
/// `hub.token` has been written. For each project row:
///   * generate a fresh token,
///   * write `hub.token.<project_id>` (0o600 on Unix),
///   * record `project_id → token` in the returned registry.
///
/// Then clean up stale files: any `hub.token.<id>` on disk whose `<id>`
/// is not among the current project rows is removed (a project that was
/// deleted since the last startup).
///
/// Soft-fail per project: a single project whose token file can't be
/// written is logged and skipped (its resolver falls back to the global
/// token) rather than aborting the whole mint — one un-writable file
/// must not strand every OTHER project's scoped token. A DB read error
/// returns an empty registry (the hub still boots; global-token compat
/// covers every project).
pub fn mint_project_tokens(db: &Db) -> ProjectTokenRegistry {
    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            tracing::error!(
                error = %e,
                "[vct-hub] project_tokens: could not read projects for per-project \
                 token minting; resolvers fall back to the global hub.token for \
                 every project this session."
            );
            return ProjectTokenRegistry::empty();
        }
    };

    let mut map: HashMap<String, String> = HashMap::with_capacity(projects.len());
    let mut live_ids: std::collections::HashSet<String> =
        std::collections::HashSet::with_capacity(projects.len());

    for project in &projects {
        live_ids.insert(project.id.clone());
        let token = match boot_token::generate_token() {
            Ok(t) => t,
            Err(e) => {
                tracing::error!(
                    project = %project.id,
                    error = %e,
                    "[vct-hub] project_tokens: CSPRNG failed; skipping its \
                     per-project token (global hub.token still works)."
                );
                continue;
            }
        };
        let path = project_token_path(&project.id);
        if let Err(e) = boot_token::write_token_file(&path, &token) {
            tracing::error!(
                path = %path.display(),
                project = %project.id,
                error = %e,
                "[vct-hub] project_tokens: could not write the token file; this \
                 project's resolvers fall back to the global hub.token this session."
            );
            continue;
        }
        map.insert(project.id.clone(), token);
    }

    cleanup_stale_project_tokens(&live_ids);

    ProjectTokenRegistry::from_map(map)
}

/// Lazily mint a per-project token for a project ADDED while the hub was
/// already running (v0.2.77 Part 8 Task 4a).
///
/// ─── Why ────────────────────────────────────────────────────────────
/// The hub mints tokens at startup only (see the module doc). A project
/// added mid-session has no `hub.token.<id>` file, so its resolver falls
/// back to the global `hub.token`. Before the flip that "just worked"
/// (compat window). AFTER the flip the global token is refused on
/// `/env` + `/config`, so a mid-session-added project's resolver would be
/// stranded with a 403 until the next hub restart. Lazy-mint closes that
/// gap self-serve: on the first request for such a project, we mint its
/// scoped token, write the file, register it, and let the request
/// proceed — so the NEXT request already finds the scoped file and rides
/// the per-project token like every startup-minted project.
///
/// ─── Contract ───────────────────────────────────────────────────────
/// * `url_segment` is the raw `{id}` from the URL — either a project id
///   OR a slug (the config handler accepts both). We canonicalize via
///   `get_project` then `get_project_by_slug`, mirroring the handler.
/// * Returns `Some((canonical_id, token))` ONLY when the segment resolves
///   to a REAL project row in `launcher.db`. An UNKNOWN segment (no id
///   and no slug match) returns `None` — the caller keeps the 401/403 it
///   would otherwise return, so an attacker cannot force a token file to
///   be written for an arbitrary id.
/// * IDEMPOTENT: if the project already has a registry entry (a race with
///   another in-flight request, or startup already minted it), we return
///   the EXISTING token rather than rotating it — never overwrite a live
///   token mid-session.
/// * Soft-fail: a CSPRNG failure or an un-writable token file returns
///   `None` (the caller keeps its refusal) rather than panicking. The
///   0o600 write discipline is identical to `mint_project_tokens`.
/// * STARTUP-REGENERATION INVARIANT preserved: the file we write is
///   cleaned up / regenerated on the next startup exactly like any other
///   `hub.token.<id>` (it is keyed by the canonical id, which is a live
///   project, so `cleanup_stale_project_tokens` keeps it and the next
///   `mint_project_tokens` rotates it).
pub fn lazy_mint_for_project(
    registry: &ProjectTokenRegistry,
    db: &Db,
    url_segment: &str,
) -> Option<(String, String)> {
    // 1. Canonicalize the URL segment → a real project id. Mirror the
    //    config handler: id first, slug fallback. A DB error or a segment
    //    matching neither → None (no mint, caller keeps its refusal).
    let canonical_id = match db.get_project(url_segment) {
        Ok(Some(p)) => p.id,
        Ok(None) => match db.get_project_by_slug(url_segment) {
            Ok(Some(p)) => p.id,
            Ok(None) => return None, // genuinely unknown → stay refused.
            Err(e) => {
                tracing::error!(
                    segment = ?url_segment,
                    error = %e,
                    "[vct-hub] project_tokens: lazy-mint slug lookup failed; \
                     leaving the request refused."
                );
                return None;
            }
        },
        Err(e) => {
            tracing::error!(
                segment = ?url_segment,
                error = %e,
                "[vct-hub] project_tokens: lazy-mint id lookup failed; \
                 leaving the request refused."
            );
            return None;
        }
    };

    // 2. Idempotent: if a token already exists for the canonical id (this
    //    project was in fact registered — e.g. a concurrent request just
    //    minted it, or the caller addressed it by slug while the id form
    //    is already registered), return the existing token untouched.
    if let Some(existing) = registry.token_for(&canonical_id) {
        return Some((canonical_id, existing));
    }

    // 3. Mint + persist (0o600) + register. Soft-fail on either failure.
    let token = match boot_token::generate_token() {
        Ok(t) => t,
        Err(e) => {
            tracing::error!(
                project = %canonical_id,
                error = %e,
                "[vct-hub] project_tokens: lazy-mint CSPRNG failed; leaving the \
                 request refused."
            );
            return None;
        }
    };
    let path = project_token_path(&canonical_id);
    if let Err(e) = boot_token::write_token_file(&path, &token) {
        tracing::error!(
            path = %path.display(),
            error = %e,
            "[vct-hub] project_tokens: lazy-mint could not write the token file; \
             leaving the request refused."
        );
        return None;
    }
    registry.insert(canonical_id.clone(), token.clone());
    tracing::info!(
        project = %canonical_id,
        "[vct-hub] project_tokens: lazy-minted a per-project token (project added \
         while the hub was running); its resolver will use the scoped token on the \
         next request."
    );
    Some((canonical_id, token))
}

/// Remove `hub.token.<id>` files whose `<id>` is not in `live_ids`.
///
/// Best-effort: a read-dir failure or an un-removable file is logged and
/// ignored (a lingering token file is a defense-in-depth concern, not a
/// correctness one — the token in it is not in the in-memory registry so
/// it authenticates nothing after this startup). We deliberately do NOT
/// touch the bare `hub.token` (the global file) — the prefix match
/// requires a NON-EMPTY id after `hub.token.`, and `hub.token` itself
/// has no trailing `.<id>`, so it can never match.
fn cleanup_stale_project_tokens(live_ids: &std::collections::HashSet<String>) {
    let root = vct_launcher_core::paths::vct_root_dir();
    let entries = match std::fs::read_dir(&root) {
        Ok(e) => e,
        Err(_) => return, // dir not there yet / unreadable — nothing to clean.
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        // Must start with `hub.token.` AND have a non-empty id tail.
        let Some(id) = name.strip_prefix(PROJECT_TOKEN_PREFIX) else {
            continue;
        };
        if id.is_empty() {
            continue; // guards against a literal "hub.token." with no id.
        }
        if live_ids.contains(id) {
            continue; // still a live project — keep its freshly-minted file.
        }
        let path = entry.path();
        if let Err(e) = std::fs::remove_file(&path) {
            tracing::debug!(
                path = %path.display(),
                error = %e,
                "[vct-hub] project_tokens: could not remove stale token file; \
                 harmless (its token is not in the live registry)."
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Token-file tests mutate VCT_STATE_DIR at process scope. Serialise
    // them so parallel cargo-test runs don't observe each other. Mirrors
    // the pattern in auth.rs / paths.rs.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_state_dir<F: FnOnce(&std::path::Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        // Safety: tests are serialised by SERIALIZE; no thread
        // concurrently observes/mutates VCT_STATE_DIR.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }
        f(tmp.path());
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    fn seed_project(db: &Db, id: &str, name: &str, folder: &str) {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?2, ?4, ?4)",
                rusqlite::params![id, name, folder, now],
            )
            .unwrap();
    }

    #[test]
    fn registry_lookup_and_reverse_lookup() {
        let mut m = HashMap::new();
        m.insert("proj-a".to_string(), "token-a".to_string());
        m.insert("proj-b".to_string(), "token-b".to_string());
        let reg = ProjectTokenRegistry::from_map(m);

        // token_for / project_for_token return owned String (values live
        // behind the RwLock) — compare via as_deref().
        assert_eq!(reg.token_for("proj-a").as_deref(), Some("token-a"));
        assert_eq!(reg.token_for("proj-b").as_deref(), Some("token-b"));
        assert_eq!(reg.token_for("proj-c"), None);

        assert_eq!(reg.project_for_token("token-a").as_deref(), Some("proj-a"));
        assert_eq!(reg.project_for_token("token-b").as_deref(), Some("proj-b"));
        assert_eq!(reg.project_for_token("garbage"), None);
        assert_eq!(reg.len(), 2);
        assert!(!reg.is_empty());
    }

    #[test]
    fn empty_registry_matches_nothing() {
        let reg = ProjectTokenRegistry::empty();
        assert!(reg.is_empty());
        assert_eq!(reg.token_for("anything"), None);
        assert_eq!(reg.project_for_token("anything"), None);
    }

    #[test]
    fn mint_writes_one_file_per_project_and_populates_registry() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "pid-1", "Alpha", "/tmp/alpha");
            seed_project(&db, "pid-2", "Beta", "/tmp/beta");

            let reg = mint_project_tokens(&db);
            assert_eq!(reg.len(), 2);

            for pid in ["pid-1", "pid-2"] {
                let path = root.join(format!("hub.token.{}", pid));
                assert!(path.exists(), "token file for {} must exist", pid);
                let on_disk = std::fs::read_to_string(&path).unwrap();
                assert_eq!(
                    reg.token_for(pid).as_deref(),
                    Some(on_disk.trim()),
                    "registry token must match the file for {}",
                    pid
                );
                assert_eq!(on_disk.len(), boot_token::TOKEN_BYTES * 2);

                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let mode =
                        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
                    assert_eq!(mode, 0o600, "per-project token file must be 0o600");
                }
            }

            // Distinct tokens per project.
            assert_ne!(reg.token_for("pid-1"), reg.token_for("pid-2"));
            // Reverse lookup pins each token to its project.
            let ta = reg.token_for("pid-1").unwrap();
            assert_eq!(reg.project_for_token(&ta).as_deref(), Some("pid-1"));
        });
    }

    #[test]
    fn mint_regenerates_tokens_on_each_startup() {
        with_state_dir(|_root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "pid-rot", "Rot", "/tmp/rot");

            let reg1 = mint_project_tokens(&db);
            let t1 = reg1.token_for("pid-rot").unwrap().to_string();
            let reg2 = mint_project_tokens(&db);
            let t2 = reg2.token_for("pid-rot").unwrap().to_string();

            assert_ne!(t1, t2, "each startup must rotate the per-project token");
        });
    }

    #[test]
    fn mint_cleans_up_stale_files_but_keeps_global_and_live() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "live-pid", "Live", "/tmp/live");

            // A leftover token file for a project that no longer exists.
            let stale = root.join("hub.token.dead-pid");
            std::fs::write(&stale, "stale-token").unwrap();
            // The bare global token file — must be preserved.
            let global = root.join("hub.token");
            std::fs::write(&global, "global-token").unwrap();

            let reg = mint_project_tokens(&db);

            assert!(
                !stale.exists(),
                "stale per-project token file must be removed"
            );
            assert!(
                root.join("hub.token.live-pid").exists(),
                "live project's token file must be written"
            );
            assert!(
                global.exists(),
                "bare global hub.token must NEVER be removed by cleanup"
            );
            assert_eq!(
                std::fs::read_to_string(&global).unwrap(),
                "global-token",
                "global hub.token content must be untouched by cleanup"
            );
            assert_eq!(reg.len(), 1);
        });
    }

    #[test]
    fn mint_with_no_projects_yields_empty_registry_and_cleans_all() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            // A stray per-project file with no live project.
            std::fs::write(root.join("hub.token.orphan"), "x").unwrap();

            let reg = mint_project_tokens(&db);
            assert!(reg.is_empty());
            assert!(
                !root.join("hub.token.orphan").exists(),
                "orphan file cleaned when no projects are live"
            );
        });
    }

    // ── v0.2.77 Part 8 Task 4a: lazy-mint ─────────────────────────────

    /// The registry is now interior-mutable: a live `insert` is visible
    /// to a subsequent read on the SAME (cloned) handle. This is the
    /// property lazy-mint relies on — the middleware clones the registry
    /// per request, so a write on one clone must be seen by the next.
    #[test]
    fn registry_insert_is_visible_across_clones() {
        let reg = ProjectTokenRegistry::empty();
        let clone = reg.clone();
        assert_eq!(reg.token_for("p"), None);
        clone.insert("p".to_string(), "tok".to_string());
        // The write via `clone` is visible via `reg` (shared Arc<RwLock>).
        assert_eq!(reg.token_for("p").as_deref(), Some("tok"));
        assert_eq!(reg.project_for_token("tok").as_deref(), Some("p"));
    }

    /// Lazy-mint for a project ADDED mid-session (a DB row exists but no
    /// registry entry): mints the token, writes the 0o600 file, registers
    /// it, and returns (canonical_id, token).
    #[test]
    fn lazy_mint_writes_file_and_registers_for_db_known_project() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "added-pid", "Added", "/tmp/added");
            // Empty registry (project appeared AFTER startup mint).
            let reg = ProjectTokenRegistry::empty();

            let out = lazy_mint_for_project(&reg, &db, "added-pid")
                .expect("db-known project must lazy-mint");
            assert_eq!(out.0, "added-pid");

            let path = root.join("hub.token.added-pid");
            assert!(path.exists(), "lazy-minted token file must be written");
            let on_disk = std::fs::read_to_string(&path).unwrap();
            assert_eq!(on_disk.trim(), out.1, "file must match returned token");
            assert_eq!(reg.token_for("added-pid").as_deref(), Some(out.1.as_str()));

            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
                assert_eq!(mode, 0o600, "lazy-minted file must be 0o600");
            }
        });
    }

    /// Lazy-mint resolves a SLUG segment to the canonical id (the config
    /// handler accepts id-OR-slug, so lazy-mint must too).
    #[test]
    fn lazy_mint_resolves_slug_to_canonical_id() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            // seed_project sets slug == name (see the INSERT).
            seed_project(&db, "slug-pid", "myslug", "/tmp/slug");
            let reg = ProjectTokenRegistry::empty();

            let out = lazy_mint_for_project(&reg, &db, "myslug")
                .expect("slug must resolve + mint");
            assert_eq!(out.0, "slug-pid", "returns the canonical id, not the slug");
            // File is keyed by the canonical id, NOT the slug.
            assert!(root.join("hub.token.slug-pid").exists());
            assert!(!root.join("hub.token.myslug").exists());
        });
    }

    /// An UNKNOWN segment (no id and no slug match) does NOT mint — the
    /// caller keeps its 401/403. This is the "attacker can't force a token
    /// file for an arbitrary id" guard.
    #[test]
    fn lazy_mint_refuses_unknown_segment() {
        with_state_dir(|root| {
            let db = Db::open_in_memory().unwrap();
            let reg = ProjectTokenRegistry::empty();

            assert!(lazy_mint_for_project(&reg, &db, "ghost").is_none());
            assert!(!root.join("hub.token.ghost").exists());
            assert!(reg.is_empty());
        });
    }

    /// Idempotent: lazy-mint for a project that ALREADY has a registry
    /// entry returns the EXISTING token, never rotating it (a live token
    /// must not change mid-session under a race).
    #[test]
    fn lazy_mint_is_idempotent_returns_existing_token() {
        with_state_dir(|_root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "idem-pid", "Idem", "/tmp/idem");
            let reg = ProjectTokenRegistry::empty();

            let first = lazy_mint_for_project(&reg, &db, "idem-pid").unwrap();
            let second = lazy_mint_for_project(&reg, &db, "idem-pid").unwrap();
            assert_eq!(first.1, second.1, "second lazy-mint must NOT rotate the token");
        });
    }

    /// STARTUP-REGENERATION INVARIANT: a lazy-minted token is rotated by
    /// the next startup mint exactly like a startup-minted one (the file
    /// is keyed by a live project id, so cleanup keeps it and the next
    /// mint overwrites it with a fresh token).
    #[test]
    fn lazy_minted_token_is_regenerated_on_next_startup() {
        with_state_dir(|_root| {
            let db = Db::open_in_memory().unwrap();
            seed_project(&db, "rot-pid", "Rot", "/tmp/rot");
            let reg = ProjectTokenRegistry::empty();

            let lazy = lazy_mint_for_project(&reg, &db, "rot-pid").unwrap().1;
            // Next startup re-mints wholesale.
            let reg2 = mint_project_tokens(&db);
            let after = reg2.token_for("rot-pid").unwrap();
            assert_ne!(lazy, after, "startup mint must rotate the lazy-minted token");
        });
    }
}
