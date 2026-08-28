// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! V47-C (v0.2.46 Part 2 Gap C): hub endpoint that migrates secrets from a
//! caller-supplied list (typically read out of a project's `.env` by
//! install.py) into the OS keychain.
//!
//! ## Why a new endpoint
//!
//! The Tauri-side `register_secret_from_source` command in
//! `commands/secrets_import.rs` does almost the same thing — but it
//! requires a running Tauri context (it takes `tauri::State<Db>`) and a
//! "source descriptor" allowlist derived from the launcher.db's `projects`
//! table. install.py runs ON A FRESH ADOPT BEFORE the project has been
//! registered with the launcher, so the source-descriptor allowlist would
//! refuse the file. We can't fix that upstream without either:
//!   * Forcing install.py to do `launcher::register_project` first (which
//!     opens a UI / changes adopt-mode UX), or
//!   * Loosening the allowlist (which weakens the path-traversal defence).
//!
//! So this endpoint runs in the hub (no Tauri State dependency, no Db
//! gating) and accepts a caller-supplied `(key, value)` list directly.
//! Discipline: the caller (install.py) is responsible for the source
//! provenance — it's reading the project's own `.env`, which it just
//! validated as belonging to the directory the user invoked install.py
//! in. The hub doesn't try to second-guess.
//!
//! ## Routing
//!
//! Mounted under the hub-wide bearer-token gate (same `Authorization:
//! Bearer <hub.token>` as every other `/api/v1/*` route). Only same-user
//! processes can read `hub.token`, which is also the threat boundary for
//! the keychain itself — so the gate matches the underlying resource's
//! threat model.
//!
//! ## Storage shape (GAP-1, 2026-07-14 — scope is now project-aware)
//!
//! The scope is decided by ONE policy function —
//! `vct_launcher_core::db::secret_scope_policy::decide_env_migration_scope`
//! (S1) — from the request's optional `project_id`:
//!
//!   * `project_id` ABSENT → `SecretScope::Shared { SENTINEL_SHARED }`.
//!     Preserves the V47-C contract for install.py's original caller, which
//!     runs on a FRESH ADOPT before the project is registered and therefore
//!     structurally has no id to send. Root installs also land here (the
//!     orchestrator-root row is host=orchestrator_root → Shared).
//!   * `project_id` PRESENT + host = base/mao →
//!     `SecretScope::PerProject { project_id }`. The owning project's
//!     credential stays that project's — it does NOT leak into every other
//!     registered project's `/env` (the cross-tenant leak this fixes).
//!   * `project_id` PRESENT + host = orchestrator_root → `Shared` (root
//!     secrets are legitimately machine-wide, user-stated).
//!   * `project_id` PRESENT but unknown → 404 `project_not_found`, NOTHING
//!     written (a caller bug; guessing Shared would recreate the leak).
//!
//! Module ID is always `"user"` (the SecretsPanel bucket). The response's
//! `scope` field (`"shared"` | `"per_project"`) tells the caller where the
//! keys landed. For the per-project arm the handler ALSO registers a
//! `project_secret_refs` row (`resolution="keychain-per-project"`) so the
//! per-project SecretsTab lists the freshly-migrated key.
//!
//! ## Value-handling discipline
//!
//! The raw value lives in scope only inside the per-secret loop. It's
//! passed to `secrets::set` and never appears in:
//!   * The success response (which carries only the list of keys).
//!   * The failure response (errors mention KEY only).
//!   * Hub logs (no `println!` / `tracing::*` of the value).
//!
//! The same inviolable contract `secrets_import.rs` documents:
//! the launcher reads, the caller never sees the value back.

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::post,
    Json, Router,
};
use serde::{Deserialize, Serialize};

use super::modules_api::LauncherDbHandle;

/// Mirror of `SENTINEL_SHARED` in `commands/secrets_cmd.rs` and
/// `commands/secrets_import.rs`. Kept as a module-private const because
/// the two writer paths (Tauri command + this hub route) need the same
/// keychain slot, and exposing it publicly from `secrets_cmd` would leak
/// an implementation detail across crate boundaries.
const SENTINEL_SHARED: &str = "_user_shared_";

/// Module ID slot for user-shared secrets. Matches `IMPORT_MODULE_ID` in
/// `secrets_import.rs` and the bucket the SecretsPanel "Shared (this user)"
/// tab reads/writes.
const IMPORT_MODULE_ID: &str = "user";

pub fn router() -> Router<LauncherDbHandle> {
    Router::new().route("/secrets/migrate", post(migrate_secrets))
}

// ─── Request / response types ───────────────────────────────────────────

/// One secret to migrate. Both fields are caller-supplied; the hub does
/// NOT validate that `key` matches `_is_secret_shaped_env_key` — that's
/// the caller's responsibility. Validating here would make the endpoint
/// fragile to future heuristic changes and force install.py to
/// hand-shape every key it sends.
#[derive(Debug, Deserialize)]
pub struct MigrateSecretItem {
    pub key: String,
    pub value: String,
}

#[derive(Debug, Deserialize)]
pub struct MigrateSecretsRequest {
    pub secrets: Vec<MigrateSecretItem>,
    /// GAP-1 (2026-07-14): the launcher.db project id that OWNS this `.env`.
    /// Per-REQUEST (all three callers migrate exactly one project's `.env`
    /// per call). ABSENT → back-compat Shared scope (old callers, and
    /// install.py's pre-registration fresh-adopt contract). PRESENT → the
    /// scope policy (S1) decides Shared (root) vs PerProject (base/mao); an
    /// unknown id is a hard 404, nothing written.
    #[serde(default)]
    pub project_id: Option<String>,
}

/// Per-failure detail returned in the `failed` array. NEVER includes the
/// raw value — only the key + error message. Matches the value-handling
/// contract `secrets_import.rs` documents.
///
/// `Deserialize` is `cfg(test)` only — the production handler only ever
/// emits this type (never reads it back). Tests round-trip it.
#[cfg_attr(test, derive(Deserialize))]
#[derive(Debug, Serialize)]
pub struct MigrateFailure {
    pub key: String,
    pub error: String,
}

/// 200-OK response shape. Always emitted — even when EVERY secret failed
/// — because partial-success is a normal outcome (e.g. one key collides
/// with a daemon hiccup but the rest land). Caller code routes on the
/// presence of entries in `failed`, not on the HTTP status.
///
/// `Deserialize` is `cfg(test)` only — same rationale as `MigrateFailure`.
#[cfg_attr(test, derive(Deserialize))]
#[derive(Debug, Serialize)]
pub struct MigrateSecretsResponse {
    pub migrated: Vec<String>,
    pub failed: Vec<MigrateFailure>,
    /// GAP-1 (2026-07-14): where the keys landed — `"shared"` or
    /// `"per_project"`. Additive: old callers (install.py pre-GAP-1) read
    /// only `migrated`/`failed` and ignore this; new callers use it for
    /// user-facing copy AND to detect an old hub (a response WITHOUT `scope`
    /// means the hub predates per-project migration → keys went to Shared).
    pub scope: String,
}

// ─── Error helpers ──────────────────────────────────────────────────────
//
// Match the envelope shape `modules_api::error_response` uses so callers
// (install.py, future v47-G-final GUI) see a consistent error contract
// across all hub routes.

// v0.2.54 Track J: error_response moved to the shared
// `crate::http_error` module (was four byte-identical copies).
use crate::http_error::error_response;

// ─── Validation ─────────────────────────────────────────────────────────

/// Validate a caller-supplied key looks like a real env var name.
///
/// Rules (matches `is_valid_env_key` in `secrets_import.rs`):
///   * First char: uppercase letter or underscore
///   * Rest: uppercase letters, digits, underscore
///   * Length 1..=128 (defensive — keychain entry names have OS limits;
///     128 is well below libsecret's 255 and Credential Manager's 256)
fn is_valid_env_key(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.is_empty() || bytes.len() > 128 {
        return false;
    }
    if !(bytes[0].is_ascii_uppercase() || bytes[0] == b'_') {
        return false;
    }
    for &b in &bytes[1..] {
        if !(b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_') {
            return false;
        }
    }
    true
}

// ─── Handler ────────────────────────────────────────────────────────────

async fn migrate_secrets(
    State(_h): State<LauncherDbHandle>,
    body: Result<Json<MigrateSecretsRequest>, axum::extract::rejection::JsonRejection>,
) -> impl IntoResponse {
    // Body parse failure → 400 with a useful diagnostic. axum's default
    // is plain-text 400; we keep the structured envelope.
    let Json(req) = match body {
        Ok(b) => b,
        Err(rej) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_body",
                format!("could not parse request body: {}", rej),
            );
        }
    };

    if req.secrets.is_empty() {
        // Defensive: not an error per se (caller may have stripped all
        // candidates upstream), but an empty migration is informative
        // enough to return as a 400 so install.py doesn't quietly assume
        // "migrated zero" was the expected outcome.
        return error_response(
            StatusCode::BAD_REQUEST,
            "empty_request",
            "secrets list is empty; nothing to migrate",
        );
    }

    // GAP-1 (2026-07-14): decide the destination scope ONCE, via the shared
    // policy (S1). An explicit-but-unknown project_id is a hard 404 — nothing
    // is written, because a silent Shared write would recreate the exact
    // cross-tenant leak this endpoint's fix prevents. The error carries the
    // id only (never a secret value).
    let migration_scope = match vct_launcher_core::db::secret_scope_policy::decide_env_migration_scope(
        &_h.0,
        req.project_id.as_deref(),
    ) {
        Ok(s) => s,
        Err(e) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "project_not_found",
                format!("cannot resolve migration scope: {}", e),
            );
        }
    };

    // Static scope descriptor for the write + active-flag + response, so the
    // per-item loop below never re-derives the branch.
    use vct_launcher_core::db::secret_scope_policy::EnvMigrationScope;
    let (scope_str, slot_project_id, response_scope) = match &migration_scope {
        EnvMigrationScope::Shared => ("shared", SENTINEL_SHARED, "shared"),
        EnvMigrationScope::PerProject(pid) => ("per_project", pid.as_str(), "per_project"),
    };

    let mut migrated: Vec<String> = Vec::with_capacity(req.secrets.len());
    let mut failed: Vec<MigrateFailure> = Vec::new();

    for item in req.secrets {
        // Validate key shape FIRST so a malformed key doesn't reach the
        // keychain layer (where it'd surface as a less-readable
        // `keyring::Error::Invalid` deep in libsecret).
        if !is_valid_env_key(&item.key) {
            failed.push(MigrateFailure {
                key: item.key.clone(),
                error: format!(
                    "invalid env key shape: {:?} (must match \
                     ^[A-Z_][A-Z0-9_]*$, length ≤ 128)",
                    item.key
                ),
            });
            continue;
        }

        // Reject empty values — there's no point migrating ""; downstream
        // resolvers can't tell that from "key not set" and we'd be hiding
        // a probable user mistake.
        if item.value.is_empty() {
            failed.push(MigrateFailure {
                key: item.key.clone(),
                error: format!("empty value for key {:?}; not migrated", item.key),
            });
            continue;
        }

        // GAP-1: write to the scope S1 decided (Shared or the owning
        // project's PerProject slot). Both arms go through the SAME guarded
        // `secrets::set` chokepoint (v0.2.80 write-guard applies
        // automatically — do NOT use `set_allowing_multiline`).
        let scope = match &migration_scope {
            EnvMigrationScope::Shared => vct_launcher_core::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            },
            EnvMigrationScope::PerProject(pid) => {
                vct_launcher_core::secrets::SecretScope::PerProject { project_id: pid }
            }
        };
        match vct_launcher_core::secrets::set(scope, IMPORT_MODULE_ID, &item.key, &item.value) {
            Ok(()) => {
                // Mark active in the DB so the SecretsPanel renders the
                // entry as "set + active" immediately. Mirrors the post-
                // write step in `register_secret_from_source` (shared arm)
                // and the manual add-form (per-project arm).
                //
                // E-4 (v0.2.73): a `mark_secret_active` failure is NOT
                // silently swallowed. The prior code logged + counted the
                // item as `migrated`, relying on an "on next launcher start"
                // reconciliation that DOES NOT EXIST for secret active-flags
                // (the only startup reconcilers walk `module_installs`, not
                // `secret_active_state`). Result: the value sat in the
                // keychain but was BOTH un-served by `/env`'s active gate AND
                // un-listed in the GUI — the user re-enters it, or the
                // migration silently under-delivers.
                //
                // Correct posture: the keychain WRITE succeeded but the
                // migration ITEM did not fully succeed (the active-flag is
                // half of "migrated + usable"). Surface it in `failed[]` so
                // the caller can retry rather than believe it succeeded. The
                // keychain value is left in place (idempotent: a retry's
                // `secrets::set` overwrites it, then re-attempts the flag).
                if let Err(e) = _h.0.mark_secret_active(
                    scope_str,
                    slot_project_id,
                    IMPORT_MODULE_ID,
                    &item.key,
                ) {
                    tracing::error!(
                        key = ?item.key,
                        error = %e,
                        "[vct-hub secrets_api] mark_secret_active failed (keychain \
                         write succeeded but the entry is un-served + un-listed \
                         until the active flag is set; reporting as failed so the \
                         caller retries)"
                    );
                    failed.push(MigrateFailure {
                        key: item.key,
                        error: format!(
                            "keychain write succeeded but marking the secret \
                             active failed: {} (the value is stored but not yet \
                             served/listed; retry to re-attempt the active flag)",
                            e
                        ),
                    });
                    continue;
                }

                // GAP-3 fold-in (per-project arm only): register a
                // `project_secret_refs` row so the per-project SecretsTab
                // (which renders from `list_project_secret_refs`) LISTS the
                // migrated key. Shared entries are deliberately ref-less (the
                // hub /env 4th loop enumerates active-state, not refs, for
                // shared/global keys). A ref failure is treated like a
                // mark-active failure: item into failed[], keychain value
                // left in place (retry is idempotent).
                if let EnvMigrationScope::PerProject(pid) = &migration_scope {
                    if let Err(e) = _h.0.set_project_secret_ref(
                        pid,
                        &item.key,
                        "keychain-per-project",
                        None,
                        Some(&item.key),
                        Some("user"),
                        &[],
                        "Migrated from .env (V47-C keychain migration)",
                        Some(true),
                    ) {
                        tracing::error!(
                            key = ?item.key,
                            error = %e,
                            "[vct-hub secrets_api] set_project_secret_ref failed \
                             (keychain write + active-flag succeeded but the \
                             per-project ref row did not land, so the SecretsTab \
                             would not list it; reporting as failed so the caller \
                             retries)"
                        );
                        failed.push(MigrateFailure {
                            key: item.key,
                            error: format!(
                                "keychain write + active flag succeeded but \
                                 registering the per-project secret ref failed: \
                                 {} (the value is stored + served but not yet \
                                 listed in the SecretsTab; retry to re-attempt \
                                 the ref)",
                                e
                            ),
                        });
                        continue;
                    }
                }

                migrated.push(item.key);
            }
            Err(e) => {
                failed.push(MigrateFailure {
                    key: item.key,
                    error: format!("keychain write failed: {}", e),
                });
            }
        }
    }

    (
        StatusCode::OK,
        Json(MigrateSecretsResponse {
            migrated,
            failed,
            scope: response_scope.to_string(),
        }),
    )
        .into_response()
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use vct_launcher_core::db::Db;
    use vct_launcher_core::secrets;

    /// Spawn a tokio HTTP server hosting just this module's router.
    /// Mirrors the `spawn_modules_api_hub` pattern in `modules_api::tests`.
    async fn spawn_secrets_api_hub() -> (String, LauncherDbHandle) {
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));
        let app: axum::Router =
            axum::Router::new().nest("/api/v1", super::router().with_state(handle.clone()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (format!("http://{}/api/v1", addr), handle)
    }

    // ─── is_valid_env_key ─────────────────────────────────────────────

    #[test]
    fn is_valid_env_key_accepts_canonical_shapes() {
        assert!(is_valid_env_key("GITHUB_TOKEN"));
        assert!(is_valid_env_key("OPENAI_API_KEY"));
        assert!(is_valid_env_key("_PRIVATE"));
        assert!(is_valid_env_key("KEY"));
        assert!(is_valid_env_key("KEY_2"));
    }

    #[test]
    fn is_valid_env_key_rejects_bad_shapes() {
        assert!(!is_valid_env_key(""));
        assert!(!is_valid_env_key("lowercase"));
        assert!(!is_valid_env_key("9LEADING_DIGIT"));
        assert!(!is_valid_env_key("HAS SPACE"));
        assert!(!is_valid_env_key("HAS-DASH"));
        // Length cap — caller-supplied 129-char key is rejected.
        let too_long: String = "A".repeat(129);
        assert!(!is_valid_env_key(&too_long));
        // Boundary: exactly 128 → accepted.
        let max: String = "A".repeat(128);
        assert!(is_valid_env_key(&max));
    }

    // ─── End-to-end via spawned hub ───────────────────────────────────

    #[tokio::test]
    async fn migrate_rejects_empty_secrets_list_with_400() {
        let (base, _h) = spawn_secrets_api_hub().await;
        let resp = reqwest::Client::new()
            .post(format!("{}/secrets/migrate", base))
            .json(&serde_json::json!({"secrets": []}))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 400);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("empty_request")
        );
    }

    #[tokio::test]
    async fn migrate_rejects_malformed_body_with_400() {
        let (base, _h) = spawn_secrets_api_hub().await;
        // Caller sent a string where an object is expected — axum's
        // JsonRejection should map to our `invalid_body` envelope.
        let resp = reqwest::Client::new()
            .post(format!("{}/secrets/migrate", base))
            .header("content-type", "application/json")
            .body("\"not an object\"")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 400);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("invalid_body")
        );
    }

    #[tokio::test]
    async fn migrate_writes_valid_secrets_to_mock_keychain() {
        // The mock keychain is thread-local. Wrap the request inside a
        // `MockGuard` scope; the spawned hub task runs on a different
        // tokio task but since axum's serve waits on the listener and
        // we drive a single request from THIS task, we need a different
        // strategy: enable the mock on the SERVER's task by entering
        // the MockGuard before the request and having the server-side
        // handler observe it via the SAME thread.
        //
        // Workaround: bind the listener and serve synchronously from a
        // blocking accept loop on the same task. Simpler: skip the
        // spawned-hub pattern for this test and call the handler
        // function directly.
        //
        // We pin the contract at the unit level here. The end-to-end
        // happy path is covered by integration tests against the real
        // hub during V47-G-final.
        let _g = secrets::for_tests::MockGuard::new();

        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        // Invoke the handler directly with a constructed Json body.
        let req = MigrateSecretsRequest {
            secrets: vec![
                MigrateSecretItem {
                    key: "GITHUB_TOKEN".to_string(),
                    value: "ghp_abc123".to_string(),
                },
                MigrateSecretItem {
                    key: "OPENAI_API_KEY".to_string(),
                    value: "sk-def456".to_string(),
                },
            ],
            project_id: None,
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let resp = migrate_secrets(State(handle), body_ok).await.into_response();
        assert_eq!(resp.status(), StatusCode::OK);

        // Extract JSON body via the response's body reader.
        let body_bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
            .await
            .expect("body read");
        let body: MigrateSecretsResponse =
            serde_json::from_slice(&body_bytes).expect("response parses");
        assert_eq!(
            body.migrated,
            vec!["GITHUB_TOKEN".to_string(), "OPENAI_API_KEY".to_string()]
        );
        assert!(body.failed.is_empty(), "no failures: {:?}", body.failed);
        // GAP-1: absent project_id → Shared scope in the response.
        assert_eq!(body.scope, "shared");

        // Verify the keychain mock got the writes at the canonical slot.
        let scope = secrets::SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let v1 = secrets::get(scope, IMPORT_MODULE_ID, "GITHUB_TOKEN").unwrap();
        assert_eq!(v1.as_deref(), Some("ghp_abc123"));
        let v2 = secrets::get(scope, IMPORT_MODULE_ID, "OPENAI_API_KEY").unwrap();
        assert_eq!(v2.as_deref(), Some("sk-def456"));
    }

    #[tokio::test]
    async fn migrate_reports_bad_key_shape_in_failed_list() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        let req = MigrateSecretsRequest {
            secrets: vec![
                MigrateSecretItem {
                    key: "GITHUB_TOKEN".to_string(),
                    value: "good".to_string(),
                },
                MigrateSecretItem {
                    key: "lowercase".to_string(),
                    value: "rejected".to_string(),
                },
                MigrateSecretItem {
                    key: "HAS SPACE".to_string(),
                    value: "rejected".to_string(),
                },
            ],
            project_id: None,
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let resp = migrate_secrets(State(handle), body_ok).await.into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        let body_bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let body: MigrateSecretsResponse = serde_json::from_slice(&body_bytes).unwrap();

        assert_eq!(body.migrated, vec!["GITHUB_TOKEN".to_string()]);
        assert_eq!(body.failed.len(), 2);
        let failed_keys: Vec<&str> = body.failed.iter().map(|f| f.key.as_str()).collect();
        assert!(failed_keys.contains(&"lowercase"));
        assert!(failed_keys.contains(&"HAS SPACE"));
        // Error messages mention the key but NEVER the value.
        for f in &body.failed {
            assert!(f.error.contains(&f.key) || f.error.contains("invalid env key"));
            assert!(!f.error.contains("rejected"), "error leaked value: {}", f.error);
        }
    }

    #[tokio::test]
    async fn migrate_reports_mark_active_failure_in_failed_list() {
        // E-4 (v0.2.73): keychain write succeeds but mark_secret_active fails
        // → the item lands in `failed[]`, NOT `migrated[]`, so the caller can
        // retry rather than believe the secret is served/listed. We force the
        // DB write to fail by dropping the table it targets.
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        // Sabotage the active-flag write path: remove the table
        // mark_secret_active INSERTs into. The keychain set (mock) still
        // succeeds, so this isolates the mark-active failure.
        handle
            .0
            .lock()
            .execute("DROP TABLE secret_active_state", [])
            .expect("drop table");

        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: "GITHUB_TOKEN".to_string(),
                value: "ghp_secretvalue".to_string(),
            }],
            project_id: None,
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let resp = migrate_secrets(State(handle.clone()), body_ok)
            .await
            .into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        let body_bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let body: MigrateSecretsResponse = serde_json::from_slice(&body_bytes).unwrap();

        // Item did NOT count as migrated — it is in failed[].
        assert!(
            body.migrated.is_empty(),
            "mark-active failure must NOT report the key as migrated: {:?}",
            body.migrated
        );
        assert_eq!(body.failed.len(), 1);
        assert_eq!(body.failed[0].key, "GITHUB_TOKEN");
        assert!(
            body.failed[0].error.contains("marking the secret active failed"),
            "error should explain the mark-active failure: {}",
            body.failed[0].error
        );
        // The error NEVER leaks the secret value.
        assert!(
            !body.failed[0].error.contains("ghp_secretvalue"),
            "error leaked the secret value: {}",
            body.failed[0].error
        );

        // The keychain WRITE did happen (idempotent for a retry).
        let scope = secrets::SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let v = secrets::get(scope, IMPORT_MODULE_ID, "GITHUB_TOKEN").unwrap();
        assert_eq!(v.as_deref(), Some("ghp_secretvalue"));
    }

    #[tokio::test]
    async fn migrate_rejects_empty_value_per_entry() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: "VALID_KEY".to_string(),
                value: "".to_string(),
            }],
            project_id: None,
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let resp = migrate_secrets(State(handle), body_ok).await.into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        let body_bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let body: MigrateSecretsResponse = serde_json::from_slice(&body_bytes).unwrap();
        assert!(body.migrated.is_empty());
        assert_eq!(body.failed.len(), 1);
        assert_eq!(body.failed[0].key, "VALID_KEY");
        assert!(body.failed[0].error.contains("empty value"));
    }

    #[tokio::test]
    async fn migrate_surfaces_keychain_set_failure() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        // Inject a one-shot failure on the next `secrets::set` for this key.
        secrets::for_tests::fail_next_set("FAILING_KEY");

        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: "FAILING_KEY".to_string(),
                value: "doesnt_matter".to_string(),
            }],
            project_id: None,
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let resp = migrate_secrets(State(handle), body_ok).await.into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        let body_bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let body: MigrateSecretsResponse = serde_json::from_slice(&body_bytes).unwrap();
        assert!(body.migrated.is_empty());
        assert_eq!(body.failed.len(), 1);
        assert_eq!(body.failed[0].key, "FAILING_KEY");
        assert!(body.failed[0].error.contains("keychain write failed"));
        // Value must NEVER appear in the error.
        assert!(
            !body.failed[0].error.contains("doesnt_matter"),
            "error leaked value: {}",
            body.failed[0].error
        );
    }

    // ─── GAP-1: project-aware scope routing ───────────────────────────

    use vct_launcher_core::db::models::ProjectHost;
    use vct_launcher_core::db::secret_active::resolve_active_user_secret_pairs_for_requester;

    fn seed_project(db: &Db, id: &str, host: ProjectHost) {
        db.insert_project(id, id, &format!("/tmp/{}", id), host, id)
            .expect("insert_project");
    }

    fn migrate_one(
        handle: &LauncherDbHandle,
        key: &str,
        value: &str,
        project_id: Option<&str>,
    ) -> MigrateSecretsResponse {
        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: key.to_string(),
                value: value.to_string(),
            }],
            project_id: project_id.map(str::to_string),
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        // Drive synchronously on the CURRENT thread via a current-thread
        // runtime so the thread-local MockGuard set by the caller stays in
        // scope for the whole handler + body read. `migrate_secrets` never
        // spawns tasks, so the future never migrates off this thread.
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let body_bytes = rt.block_on(async {
            let resp = migrate_secrets(State(handle.clone()), body_ok)
                .await
                .into_response();
            axum::body::to_bytes(resp.into_body(), 1024 * 1024).await
        });
        serde_json::from_slice(&body_bytes.unwrap()).unwrap()
    }

    #[test]
    fn migrate_with_project_id_writes_per_project_scope() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        seed_project(&db, "proj-a", ProjectHost::Base);
        let handle = LauncherDbHandle(Arc::new(db));

        let body = migrate_one(&handle, "CLIENTA_DB_PASSWORD", "pw-a", Some("proj-a"));
        assert_eq!(body.migrated, vec!["CLIENTA_DB_PASSWORD".to_string()]);
        assert!(body.failed.is_empty(), "failures: {:?}", body.failed);
        assert_eq!(body.scope, "per_project");

        // (a) value readable at PerProject, ABSENT at Shared.
        let per = secrets::SecretScope::PerProject { project_id: "proj-a" };
        assert_eq!(
            secrets::get(per, IMPORT_MODULE_ID, "CLIENTA_DB_PASSWORD").unwrap().as_deref(),
            Some("pw-a")
        );
        let shared = secrets::SecretScope::Shared { project_id: SENTINEL_SHARED };
        assert_eq!(
            secrets::get(shared, IMPORT_MODULE_ID, "CLIENTA_DB_PASSWORD").unwrap(),
            None,
            "per-project migrate must NOT write the shared slot"
        );

        // (b) active for proj-a, NOT resolvable for another project.
        let own = resolve_active_user_secret_pairs_for_requester(&handle.0, "proj-a", "proj-a");
        assert!(own.iter().any(|(k, _)| k == "CLIENTA_DB_PASSWORD"));
        let other = resolve_active_user_secret_pairs_for_requester(&handle.0, "other", "other");
        assert!(
            !other.iter().any(|(k, _)| k == "CLIENTA_DB_PASSWORD"),
            "per-project secret leaked into another project's /env: {:?}",
            other
        );

        // (c) project_secret_refs row exists with keychain-per-project.
        let refs = handle.0.list_project_secret_refs("proj-a").unwrap();
        let r = refs
            .iter()
            .find(|r| r.secret_key == "CLIENTA_DB_PASSWORD")
            .expect("ref row for migrated key");
        assert_eq!(r.resolution, "keychain-per-project");
        assert!(r.is_set, "migrated per-project ref must be is_set=true");
    }

    #[test]
    fn migrate_orchestrator_root_project_stays_shared() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        seed_project(&db, "proj-root", ProjectHost::OrchestratorRoot);
        let handle = LauncherDbHandle(Arc::new(db));

        let body = migrate_one(&handle, "ROOT_SHARED_KEY", "rv", Some("proj-root"));
        assert_eq!(body.migrated, vec!["ROOT_SHARED_KEY".to_string()]);
        assert_eq!(body.scope, "shared");

        // Landed in Shared, NOT PerProject; no ref row.
        let shared = secrets::SecretScope::Shared { project_id: SENTINEL_SHARED };
        assert_eq!(
            secrets::get(shared, IMPORT_MODULE_ID, "ROOT_SHARED_KEY").unwrap().as_deref(),
            Some("rv")
        );
        let refs = handle.0.list_project_secret_refs("proj-root").unwrap();
        assert!(
            !refs.iter().any(|r| r.secret_key == "ROOT_SHARED_KEY"),
            "shared (root) migrate must not register a per-project ref"
        );
    }

    #[test]
    fn migrate_unknown_project_id_404s_and_writes_nothing() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: "SHOULD_NOT_LAND".to_string(),
                value: "leak-me".to_string(),
            }],
            project_id: Some("ghost-project".to_string()),
        };
        let body_ok: Result<
            Json<MigrateSecretsRequest>,
            axum::extract::rejection::JsonRejection,
        > = Ok(Json(req));
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let (status, body_bytes) = rt.block_on(async {
            let resp = migrate_secrets(State(handle.clone()), body_ok)
                .await
                .into_response();
            let status = resp.status();
            let bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
                .await
                .unwrap();
            (status, bytes)
        });
        assert_eq!(status, StatusCode::NOT_FOUND);
        let body: serde_json::Value = serde_json::from_slice(&body_bytes).unwrap();
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("project_not_found")
        );
        // Error must never leak the value.
        let text = String::from_utf8_lossy(&body_bytes);
        assert!(!text.contains("leak-me"), "404 leaked the value: {}", text);

        // NOTHING written in either slot; no active rows.
        let shared = secrets::SecretScope::Shared { project_id: SENTINEL_SHARED };
        assert_eq!(
            secrets::get(shared, IMPORT_MODULE_ID, "SHOULD_NOT_LAND").unwrap(),
            None
        );
    }

    #[test]
    fn migrate_per_project_ref_failure_lands_in_failed() {
        // Keychain write + mark-active succeed, but the ref row insert fails
        // (table dropped). The item must land in failed[], never migrated[],
        // and the keychain value stays (retry-idempotent).
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        seed_project(&db, "proj-b", ProjectHost::Base);
        db.lock()
            .execute("DROP TABLE project_secret_refs", [])
            .expect("drop table");
        let handle = LauncherDbHandle(Arc::new(db));

        let body = migrate_one(&handle, "REF_FAIL_KEY", "vv", Some("proj-b"));
        assert!(
            body.migrated.is_empty(),
            "ref failure must NOT count as migrated: {:?}",
            body.migrated
        );
        assert_eq!(body.failed.len(), 1);
        assert_eq!(body.failed[0].key, "REF_FAIL_KEY");
        assert!(
            body.failed[0].error.contains("per-project secret ref"),
            "error should explain the ref failure: {}",
            body.failed[0].error
        );
        assert!(!body.failed[0].error.contains("vv"), "error leaked value");

        // Keychain value present (retry-idempotent).
        let per = secrets::SecretScope::PerProject { project_id: "proj-b" };
        assert_eq!(
            secrets::get(per, IMPORT_MODULE_ID, "REF_FAIL_KEY").unwrap().as_deref(),
            Some("vv")
        );
    }
}
