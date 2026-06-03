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
//! ## Storage shape
//!
//! Migrated secrets land in:
//!   * Scope:     `SecretScope::Shared { project_id: SENTINEL_SHARED }`
//!   * Module ID: `"user"`
//!   * Key:       the original env var name (e.g. `OPENAI_API_KEY`)
//!
//! This is the same `(scope, module_id)` tuple `register_secret_from_source`
//! and the SecretsPanel "Shared (this user)" tab use, so the migrated
//! values become immediately visible/editable in the launcher GUI.
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
}

// ─── Error helpers ──────────────────────────────────────────────────────
//
// Match the envelope shape `modules_api::error_response` uses so callers
// (install.py, future v47-G-final GUI) see a consistent error contract
// across all hub routes.

fn error_response(
    status: StatusCode,
    code: &str,
    message: impl Into<String>,
) -> axum::response::Response {
    (
        status,
        Json(serde_json::json!({
            "error": {
                "code": code,
                "message": message.into(),
            }
        })),
    )
        .into_response()
}

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

        // Write to the shared user-bucket (matches `secrets_import.rs`).
        let scope = vct_launcher_core::secrets::SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        match vct_launcher_core::secrets::set(scope, IMPORT_MODULE_ID, &item.key, &item.value) {
            Ok(()) => {
                // Mark active in the DB so the SecretsPanel renders the
                // entry as "set + active" immediately. Mirrors the post-
                // write step in `register_secret_from_source`.
                //
                // Best-effort: a DB write failure here doesn't undo the
                // keychain set (irreversible from this call site without
                // an explicit `secrets::delete`, which has its own
                // failure modes). We log + continue; the SecretsPanel
                // will pick the entry up on next launcher startup via
                // its on-launch reconciliation.
                if let Err(e) = _h.0.mark_secret_active(
                    "shared",
                    SENTINEL_SHARED,
                    IMPORT_MODULE_ID,
                    &item.key,
                ) {
                    eprintln!(
                        "[vct-hub secrets_api] mark_secret_active failed for \
                         key {:?}: {} (keychain write succeeded; GUI will \
                         catch up on next launcher start)",
                        item.key, e,
                    );
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
        Json(MigrateSecretsResponse { migrated, failed }),
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
    async fn migrate_rejects_empty_value_per_entry() {
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        let req = MigrateSecretsRequest {
            secrets: vec![MigrateSecretItem {
                key: "VALID_KEY".to_string(),
                value: "".to_string(),
            }],
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
}
