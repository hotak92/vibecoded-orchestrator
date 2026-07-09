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
use std::sync::Arc;

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
/// Wrapped in `Arc` so the middleware closure clones cheaply (one atomic
/// increment per request). The map is immutable after construction —
/// we never mutate it while the server runs (mid-session project adds
/// wait for the next startup; see the module doc).
#[derive(Clone)]
pub struct ProjectTokenRegistry {
    /// project_id → token. Empty when no projects are registered.
    by_project: Arc<HashMap<String, String>>,
}

impl ProjectTokenRegistry {
    /// Build an empty registry (used when the DB read fails — the hub
    /// still boots; every resolver falls back to the global token).
    pub fn empty() -> Self {
        Self {
            by_project: Arc::new(HashMap::new()),
        }
    }

    /// Construct directly from a project_id → token map. Primarily for
    /// tests; production uses [`mint_project_tokens`].
    pub fn from_map(map: HashMap<String, String>) -> Self {
        Self {
            by_project: Arc::new(map),
        }
    }

    /// The token minted for `project_id`, if any. `None` when the
    /// project has no per-project token (not registered at startup, or
    /// added mid-session).
    pub fn token_for(&self, project_id: &str) -> Option<&str> {
        self.by_project.get(project_id).map(String::as_str)
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
    ///     token check (compat) → 401 if that also fails.
    pub fn project_for_token(&self, bearer: &str) -> Option<&str> {
        let mut matched: Option<&str> = None;
        for (pid, tok) in self.by_project.iter() {
            // Walk EVERY entry (no early break) so the number of
            // comparisons doesn't leak how far down the map a match sat.
            if boot_token::constant_time_eq(bearer.as_bytes(), tok.as_bytes()) {
                matched = Some(pid.as_str());
            }
        }
        matched
    }

    /// Number of registered per-project tokens. Test/diagnostic aid.
    pub fn len(&self) -> usize {
        self.by_project.len()
    }

    /// Whether the registry holds no tokens.
    pub fn is_empty(&self) -> bool {
        self.by_project.is_empty()
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
            eprintln!(
                "[vct-hub] project_tokens: could not read projects for per-project \
                 token minting ({}); resolvers fall back to the global hub.token \
                 for every project this session.",
                e
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
                eprintln!(
                    "[vct-hub] project_tokens: CSPRNG failed for project {} ({}); \
                     skipping its per-project token (global hub.token still works).",
                    project.id, e
                );
                continue;
            }
        };
        let path = project_token_path(&project.id);
        if let Err(e) = boot_token::write_token_file(&path, &token) {
            eprintln!(
                "[vct-hub] project_tokens: could not write {} ({}); project {} \
                 resolvers fall back to the global hub.token this session.",
                path.display(),
                e,
                project.id
            );
            continue;
        }
        map.insert(project.id.clone(), token);
    }

    cleanup_stale_project_tokens(&live_ids);

    ProjectTokenRegistry::from_map(map)
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
            eprintln!(
                "[vct-hub] project_tokens: could not remove stale token file {} ({}); \
                 harmless (its token is not in the live registry).",
                path.display(),
                e
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

        assert_eq!(reg.token_for("proj-a"), Some("token-a"));
        assert_eq!(reg.token_for("proj-b"), Some("token-b"));
        assert_eq!(reg.token_for("proj-c"), None);

        assert_eq!(reg.project_for_token("token-a"), Some("proj-a"));
        assert_eq!(reg.project_for_token("token-b"), Some("proj-b"));
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
                    reg.token_for(pid),
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
            let ta = reg.token_for("pid-1").unwrap().to_string();
            assert_eq!(reg.project_for_token(&ta), Some("pid-1"));
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
}
