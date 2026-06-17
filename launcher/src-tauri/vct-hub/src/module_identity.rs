// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.61 (Option H) — per-spawn module-IDENTITY tokens.
//
// The problem: a GLOBAL paid-module container (e.g. vct-rl-reranker)
// serves ALL projects from ONE process and must read its own per-project
// data from `launcher.db` THROUGH the hub (the single-writer principle
// forbids the container opening launcher.db directly). The hub route
// `GET /modules/{id}/projects/{pid}/rl/events` needs auth — but the
// container must NOT get `hub.token` (the launcher's master credential;
// handing it to a 3rd-party image is privilege escalation — see
// module_db_api.rs trust-boundary note).
//
// The per-(module,project) `module_access_tokens` table can't help: a
// global container has ONE process but serves MANY projects, so a single
// env-injected per-project token can't scope per request.
//
// Option H (the chosen design): separate IDENTITY from SCOPE.
//   * IDENTITY — a per-MODULE bearer minted HERE at container spawn, held
//     in-memory, injected as `-e VCT_MODULE_TOKEN=…`. Proves "I am module
//     X". Never persisted (no keychain, no DB) — mirrors `hub.token`
//     (CSPRNG, per-process, not user-managed).
//   * SCOPE — the per-request `{project_id}` in the URL path, authorized
//     against the access matrix (`require_module_scope`).
//
// Restart-contract self-heal (the load-bearing property):
//   * Hub restarts, container alive → the supervisor's `podman rm -f` +
//     recreate respawns the container with a freshly-minted token
//     registered in the NEW hub's set. The stale container never
//     survives. (No code here — that's the supervisor's existing behavior.)
//   * Container restarts (crash / `--restart=unless-stopped`), hub alive →
//     podman preserves the create-time `-e VCT_MODULE_TOKEN`, and THIS
//     in-memory set is intact (hub didn't restart) → same token still
//     validates.
// Both directions converge on: the token is valid iff it's in this set,
// and the set is repopulated at every (re)spawn the hub performs.

use std::collections::HashMap;
use std::sync::Mutex;

use vct_launcher_core::services::boot_token;

/// Process-global registry of live module-identity tokens.
/// Maps `token -> module_id`. Populated at container spawn, consulted by
/// `require_module_scope`. Same `static Mutex<...>` idiom as
/// `weaviate_schema_probe::SCHEMA_CACHE`.
///
/// Lifetime = the hub process. A hub restart starts with an empty map;
/// the supervisor's recreate-on-start repopulates it (see module-level
/// note). We never expire entries by time — validity is "is it in the
/// set", and the set is authoritative for the current hub process.
static MODULE_IDENTITY_TOKENS: Mutex<Option<HashMap<String, String>>> = Mutex::new(None);

/// Mint a fresh identity token for `module_id`, register it, and return
/// it for injection into the container's env.
///
/// Idempotent-ish by design: each call mints a NEW token and DROPS any
/// prior token for the same module (a re-spawn supersedes the old one —
/// the old container was `rm -f`'d, so its token should no longer
/// validate). This keeps the set from accumulating dead tokens across
/// re-spawns of the same module.
pub fn mint_and_register(module_id: &str) -> Result<String, String> {
    let token = boot_token::generate_token()?;
    let mut guard = MODULE_IDENTITY_TOKENS
        .lock()
        .map_err(|_| "module_identity: token registry mutex poisoned".to_string())?;
    let map = guard.get_or_insert_with(HashMap::new);
    // Drop any existing token(s) for this module — a re-spawn invalidates
    // the prior container's credential.
    map.retain(|_tok, owner| owner != module_id);
    map.insert(token.clone(), module_id.to_string());
    Ok(token)
}

/// Resolve a presented bearer to the `module_id` that owns it, if any.
/// `require_module_scope` calls this; a hit means "this bearer is module
/// X's live identity token". Constant-time comparison is unnecessary here
/// — the map lookup is by the full token value, and the tokens are
/// high-entropy CSPRNG output (boot_token), so a timing side-channel on a
/// HashMap probe doesn't meaningfully narrow the keyspace. (The DB-token
/// path uses the same plain-equality model.)
pub fn resolve_module(token: &str) -> Option<String> {
    let guard = MODULE_IDENTITY_TOKENS.lock().ok()?;
    guard.as_ref()?.get(token).cloned()
}

/// Drop a module's identity token (e.g. on explicit container stop /
/// uninstall). Best-effort; a poisoned mutex is a no-op (the worst case
/// is a dead token lingering until the next hub restart clears the set).
pub fn revoke(module_id: &str) {
    if let Ok(mut guard) = MODULE_IDENTITY_TOKENS.lock() {
        if let Some(map) = guard.as_mut() {
            map.retain(|_tok, owner| owner != module_id);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // These tests share the process-global map; serialize them so a
    // parallel run can't see another test's entries. (Same discipline as
    // the keychain tests' serialize lock.)
    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn mint_then_resolve_roundtrips() {
        let _g = TEST_LOCK.lock().unwrap();
        let tok = mint_and_register("vct-test-mod-a").expect("mint");
        assert_eq!(resolve_module(&tok).as_deref(), Some("vct-test-mod-a"));
        revoke("vct-test-mod-a");
    }

    #[test]
    fn unknown_token_resolves_none() {
        let _g = TEST_LOCK.lock().unwrap();
        assert_eq!(resolve_module("definitely-not-a-real-token-zzzz"), None);
    }

    #[test]
    fn respawn_supersedes_prior_token() {
        let _g = TEST_LOCK.lock().unwrap();
        let old = mint_and_register("vct-test-mod-b").expect("mint1");
        let new = mint_and_register("vct-test-mod-b").expect("mint2");
        assert_ne!(old, new, "re-mint must produce a fresh token");
        // The OLD token no longer validates (re-spawn invalidated it)…
        assert_eq!(resolve_module(&old), None, "stale token must be dropped");
        // …the NEW one does.
        assert_eq!(resolve_module(&new).as_deref(), Some("vct-test-mod-b"));
        revoke("vct-test-mod-b");
    }

    #[test]
    fn revoke_removes_token() {
        let _g = TEST_LOCK.lock().unwrap();
        let tok = mint_and_register("vct-test-mod-c").expect("mint");
        revoke("vct-test-mod-c");
        assert_eq!(resolve_module(&tok), None);
    }
}
