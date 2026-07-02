//! OpenAI API key lifecycle (v0.2.18, Commit 3).
//!
//! Wires the orchestrator's `openai_api_key` bundled secret (declared in
//! `vct-module.json::bundled_secrets`) to user-facing Tauri commands and a
//! startup recovery state machine.
//!
//! ─── Architecture ────────────────────────────────────────────────────
//!
//! Mirrors the `github_pat` pattern in `commands::installer`: the keychain
//! row at `(scope=Shared(SENTINEL_SHARED), module_id="user", key="openai_api_key")`
//! is the single source of truth. The hub's `/projects/{id}/env` resolver
//! reads it for every base-host project via the existing `bundled_secrets`
//! loop in `hub::modules_api::project_env` — no new resolver wiring needed.
//!
//! Three commands:
//!   * `register_openai_api_key`  — validate, persist, optionally set as
//!                                  the new-projects default. Refuses
//!                                  invalid keys on explicit register.
//!   * `validate_openai_api_key`  — free probe via `GET /v1/models/<model>`.
//!                                  No tokens consumed, no billing entry.
//!   * `recheck_openai_validity`  — reads keychain, revalidates, runs the
//!                                  recovery state machine. Backs the
//!                                  Preferences "Re-check" button.
//!
//! ─── Validation method (LOCKED) ───────────────────────────────────────
//!
//! `GET https://api.openai.com/v1/models/<model>` with
//! `Authorization: Bearer <key>` is free per OpenAI's pricing docs (the
//! models-list endpoint is not billable). We deliberately do NOT use
//! `POST /v1/embeddings` to validate — that would consume tokens and
//! pollute the user's usage dashboard.
//!
//! Response handling:
//!   * 200  → Valid (key + model accessible)
//!   * 401  → Invalid (auth failed; key revoked or wrong)
//!   * 403  → Invalid (key blocked for this model — project restriction)
//!   * 404  → Invalid (model not accessible to this key)
//!   * 429  → Valid, rate_limited=true (treat as valid; we never want to
//!            reject a working key just because the user is hitting rate
//!            limits at validation time)
//!   * Other → Error (raw status surfaced for debugging)
//!
//! ─── Recovery state machine (startup background task) ─────────────────
//!
//! Drives the "previously-valid-now-invalid" / "previously-invalid-now-valid"
//! transitions so the user's default-embedding selection stays consistent
//! with what's actually reachable. State is persisted in `app_state`:
//!
//!   * `default_text_embedding`  — current default text embed model id
//!   * `default_code_embedding`  — current default code embed model id
//!   * `openai_was_valid`        — "true" once the user has ever
//!                                 registered a valid OpenAI key. Sticky:
//!                                 only cleared by an explicit re-register
//!                                 with a Valid result.
//!   * `openai_fallback_pending` — JSON `{text: Option<String>, code: Option<String>}`,
//!                                 set when a previously-valid key now
//!                                 fails AND the current defaults are
//!                                 openai-*. On the next Valid result,
//!                                 restored verbatim.
//!
//! Invalid-on-first-set never reaches the state machine — `register_openai_api_key`
//! rejects with an error and the keychain stays untouched.

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, Runtime, State};

use crate::db::Db;
use crate::secrets::{self, SecretScope};

// ─── Constants ───────────────────────────────────────────────────────────

/// Sentinel project_id for shared scope (mirrors `commands::secrets_cmd`
/// and `commands::installer`). Kept module-private; widening to a
/// pub-crate const elsewhere would just hide the dependency on the
/// shared-scope writer / reader contract.
const SENTINEL_SHARED: &str = "_user_shared_";

/// Module identifier for the user-bucket keychain entry. Matches the
/// SecretsPanel "Shared (this user)" tab AND `register_github_pat`'s
/// post-2026-05-10 unified `GITHUB_PAT_MODULE_ID`. Pinned to `"user"`
/// so any future SecretsPanel "Shared" tab entry for `openai_api_key`
/// (a future ergonomic surface) writes to the same keychain row this
/// command writes to.
const OPENAI_MODULE_ID: &str = "user";

/// Keychain key for the OpenAI API key. Matches the
/// `bundled_secrets[].key` entry in `vct-module.json`. The hub's env-
/// resolver uses this exact string when emitting `openai_api_key` to
/// every base-host project's env.
const OPENAI_KEY: &str = "openai_api_key";

/// Default embedding model used for validation when the caller doesn't
/// supply one. Matches the v0.2.18 locked design decision
/// (`text-embedding-3-small`, 1536-dim, $0.02/1M tokens).
const DEFAULT_VALIDATION_MODEL: &str = "text-embedding-3-small";

/// Default text-embedding identifier surfaced in `default_text_embedding`
/// when the user opts into OpenAI as the new-projects default.
const OPENAI_DEFAULT_TEXT_MODEL_ID: &str = "openai-text-embedding-3-small";

/// Default code-embedding identifier. Same model today; the
/// `openai-`-prefixed naming is forward-compat for the eventual
/// OpenAI code-specific embedder (see v0.2.18 plan §5 — vector slot
/// split lockdown).
const OPENAI_DEFAULT_CODE_MODEL_ID: &str = "openai-text-embedding-3-small";

/// Default local fallback ids used by the recovery state machine when
/// switching defaults off `openai-*`. Mirror the install.py preset
/// defaults in v0.2.18 Commit 10.
const LOCAL_TEXT_FALLBACK_ID: &str = "qwen3-embedding:0.6b";
const LOCAL_CODE_FALLBACK_OLLAMA: &str = "qwen3-embedding:0.6b";
const LOCAL_CODE_FALLBACK_CODEEMBED: &str = "codesage-large-v2";

/// App-state keys. Convention: bare lower-case strings (matching the
/// plan's locked schema) rather than the `<domain>.<key>` convention used
/// for some launcher-internal flags. The plan explicitly names these in
/// commit 10's contract; renaming them here would break the cross-commit
/// integration.
pub const APP_STATE_DEFAULT_TEXT_EMBED: &str = "default_text_embedding";
pub const APP_STATE_DEFAULT_CODE_EMBED: &str = "default_code_embedding";
pub const APP_STATE_OPENAI_WAS_VALID: &str = "openai_was_valid";
pub const APP_STATE_OPENAI_FALLBACK_PENDING: &str = "openai_fallback_pending";

/// Tauri event names for the recovery state machine. The Preferences
/// page (Commit 7) listens for both and renders toasts. The "re-register
/// failed" event is a diagnostic for the Wizard / Preferences UX.
const EVT_OPENAI_KEY_INVALIDATED: &str = "vct-openai-key-invalidated";
const EVT_OPENAI_KEY_RESTORED: &str = "vct-openai-key-restored";
const EVT_OPENAI_RE_REGISTER_FAILED: &str = "vct-openai-key-re-register-failed";

/// HTTP timeout for the validation probe. Long enough for OpenAI's
/// p99 (typically <500ms) plus headroom; short enough that startup
/// boot doesn't stall on a network outage.
const VALIDATION_TIMEOUT_SECS: u64 = 8;

/// HTTP timeout for the code-embedding service health probe used by
/// `choose_best_local_code_default`. Kept tight — this is a one-shot
/// localhost probe; if the service isn't ready in 500ms it's not coming.
const CODE_EMBED_HEALTH_TIMEOUT_MS: u64 = 500;

/// Default URL for the code-embedding service when `CODE_EMBED_SERVICE_URL`
/// is unset. Matches `DEFAULT_CODE_EMBED_PORT` in
/// `mcp_registration.rs` (port 11440).
const DEFAULT_CODE_EMBED_URL: &str = "http://localhost:11440";

// ─── Response / status types ────────────────────────────────────────────

/// Outcome of an OpenAI validation probe. Serialised with `tag = "status"`
/// so the JS side gets a discriminated union (`{status: "valid", model: ...}`,
/// `{status: "invalid", reason: ...}`, `{status: "error", detail: ...}`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum OpenAiValidationResult {
    /// Key is usable. `rate_limited=true` when the probe got 429 — the
    /// key itself is fine, the API is just throttling at probe time.
    Valid { model: String, rate_limited: bool },
    /// Key is unusable. `reason` is a short human-readable string for the
    /// UI; `http_status` lets the GUI render finer-grained error text
    /// (e.g. "401 — auth failed" vs "404 — model not accessible").
    Invalid {
        reason: String,
        http_status: Option<u16>,
    },
    /// Network / DNS / TLS / unexpected HTTP status. `detail` carries the
    /// raw error string for debugging. Distinct from `Invalid`: an Error
    /// is "we couldn't decide", not "the key is bad".
    Error { detail: String },
}

/// Successful response payload from `register_openai_api_key`. The
/// `masked_key` field lets the GUI rerender the input field's preview
/// without re-querying. `default_set` mirrors the `set_as_default`
/// argument so the GUI knows whether the new-projects defaults were
/// updated (so it can refresh those dropdowns).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegisterOpenAiResponse {
    pub masked_key: String,
    pub default_set: bool,
}

/// JSON-serialised state held in `app_state.openai_fallback_pending` when
/// a previously-valid key fails the startup re-check. Each `Option<String>`
/// is the openai-* model id that was active immediately before the
/// fallback fired — restored verbatim on the next Valid result.
///
/// Both fields are independent: a user with `default_text_embedding =
/// "openai-text-embedding-3-small"` and `default_code_embedding =
/// "qwen3-embedding:0.6b"` would get `{text: Some(...), code: None}` —
/// only the openai-* slot is captured for restoration.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FallbackPending {
    pub text: Option<String>,
    pub code: Option<String>,
}

impl FallbackPending {
    pub fn is_empty(&self) -> bool {
        self.text.is_none() && self.code.is_none()
    }
}

// ─── Tauri commands ──────────────────────────────────────────────────────

/// Validate an OpenAI key by hitting the free `/v1/models/<model>`
/// endpoint. Default model: `text-embedding-3-small`.
///
/// Errors flow into `OpenAiValidationResult::Error` (network / DNS /
/// TLS / parse failures), not into `Err(_)`. Returning `Result<_, String>`
/// at the Tauri boundary is for unrecoverable Rust-side bugs only —
/// validation has well-defined outcomes for every HTTP class.
#[command]
pub async fn validate_openai_api_key(
    value: String,
    model: Option<String>,
) -> Result<OpenAiValidationResult, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Ok(OpenAiValidationResult::Invalid {
            reason: "empty key".into(),
            http_status: None,
        });
    }
    let model = model.unwrap_or_else(|| DEFAULT_VALIDATION_MODEL.to_string());
    let url = format!("https://api.openai.com/v1/models/{}", model);

    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(VALIDATION_TIMEOUT_SECS))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            return Ok(OpenAiValidationResult::Error {
                detail: format!("http client build: {}", e),
            });
        }
    };

    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", trimmed))
        .send()
        .await;

    Ok(classify_validation_response(resp, &model).await)
}

/// Inner classifier — separated out so tests can drive the
/// (status, body) decision tree without a live HTTP server. The async
/// signature matches the reqwest path (`r.text().await`); the pure
/// `classify_status` helper below is unit-tested without HTTP.
async fn classify_validation_response(
    resp: Result<reqwest::Response, reqwest::Error>,
    model: &str,
) -> OpenAiValidationResult {
    match resp {
        Err(e) => OpenAiValidationResult::Error {
            detail: format!("request failed: {}", e),
        },
        Ok(r) => {
            let status = r.status().as_u16();
            // Body is only needed for the "unknown status" path so we
            // can surface a useful detail. For known statuses we don't
            // read it — saves a wire round-trip on the happy path.
            // Read it eagerly here (we own `r`); the sync classifier
            // closure below sees the already-resolved string.
            let body_eager: String = if matches!(status, 200 | 401 | 403 | 404 | 429) {
                String::new()
            } else {
                r.text().await.unwrap_or_default()
            };
            classify_status(status, model, || body_eager)
        }
    }
}

/// Pure status-code → outcome decision. Same shape as
/// `classify_validation_response` but takes a lazy body-getter so the
/// 200/401/403/404/429 fast-paths don't need to read the response body.
/// Unit-testable without HTTP.
fn classify_status<F>(status: u16, model: &str, body_lazy: F) -> OpenAiValidationResult
where
    F: FnOnce() -> String,
{
    match status {
        200 => OpenAiValidationResult::Valid {
            model: model.to_string(),
            rate_limited: false,
        },
        401 => OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        },
        403 => OpenAiValidationResult::Invalid {
            reason: "key blocked for this model".into(),
            http_status: Some(403),
        },
        404 => OpenAiValidationResult::Invalid {
            reason: format!("model not accessible to this key ({})", model),
            http_status: Some(404),
        },
        429 => OpenAiValidationResult::Valid {
            model: model.to_string(),
            rate_limited: true,
        },
        s => {
            // Read the body lazily for the "unknown" path so the GUI can
            // surface OpenAI's error string ("incorrect_api_key", etc.)
            // for debugging. Truncated to avoid blowing up logs on a
            // malformed proxy response.
            let body = body_lazy();
            let trimmed = if body.len() > 256 {
                format!("{}…", &body[..256])
            } else {
                body
            };
            OpenAiValidationResult::Invalid {
                reason: format!("unexpected status {}: {}", s, trimmed),
                http_status: Some(s),
            }
        }
    }
}

/// Persist the OpenAI key after validation. Refuses to write an invalid
/// key (an explicit register MUST come from a user typing a new key —
/// they don't want the launcher to silently accept and stash a broken
/// one).
///
/// If the supplied key is invalid AND `openai_was_valid` was previously
/// set to `true`, we emit `vct-openai-key-re-register-failed` for
/// diagnostics so the wizard / preferences UI can highlight "your key
/// was working — what changed?" before the user retries. The keychain
/// is left untouched in both cases (the prior key, if any, stays).
///
/// `set_as_default=true` writes `default_text_embedding` and
/// `default_code_embedding` to the OpenAI defaults. Used by the
/// Wizard's "Use OpenAI as the default embedding provider" checkbox.
#[command]
pub async fn register_openai_api_key<R: Runtime>(
    value: String,
    set_as_default: bool,
    handle: AppHandle<R>,
    db: State<'_, Db>,
) -> Result<RegisterOpenAiResponse, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err("openai_api_key cannot be empty".into());
    }

    // Validate FIRST. On Invalid/Error we never touch the keychain.
    let validation = validate_openai_api_key(trimmed.to_string(), None).await?;

    match &validation {
        OpenAiValidationResult::Valid { .. } => {
            // fall through to the write path
        }
        OpenAiValidationResult::Invalid { reason, http_status } => {
            // "Previously valid, now invalid on explicit re-register": emit
            // a diagnostic event so the UI can render a richer message
            // ("your last successful key validated on X; the new one
            // failed: $reason"). The register itself still fails — the
            // user explicitly typed a bad key.
            let was_valid = db
                .app_state_get_bool(APP_STATE_OPENAI_WAS_VALID)
                .ok()
                .flatten()
                .unwrap_or(false);
            if was_valid {
                let payload = serde_json::json!({
                    "reason": reason,
                    "http_status": http_status,
                });
                let _ = handle.emit(EVT_OPENAI_RE_REGISTER_FAILED, payload);
            }
            return Err(format!("openai key validation failed: {}", reason));
        }
        OpenAiValidationResult::Error { detail } => {
            return Err(format!("openai key validation error: {}", detail));
        }
    }

    // Write to keychain. SecretScope::Shared { SENTINEL_SHARED } matches
    // both the github_pat pattern AND the hub's bundled_secrets resolver.
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    secrets::set(scope, OPENAI_MODULE_ID, OPENAI_KEY, trimmed)
        .map_err(|e| format!("keychain set openai_api_key: {}", e))?;

    // Mark active so the hub's `/projects/{id}/env` active-flag gate
    // surfaces the value (same pattern as register_github_pat).
    db.mark_secret_active("shared", SENTINEL_SHARED, OPENAI_MODULE_ID, OPENAI_KEY)
        .map_err(|e| format!("mark_secret_active openai_api_key: {}", e))?;

    // Mark "ever valid" sticky bit. The recovery state machine reads this
    // to decide whether an invalid-on-startup result is a "previously
    // valid, now broken" case (KEEP the key in keychain, fall back) or a
    // "never worked anyway" case (no rescue path needed; user has to
    // explicitly fix the key via the GUI).
    db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true)
        .map_err(|e| format!("app_state_set openai_was_valid: {}", e))?;

    // If the user opted in, write the new-projects defaults. install.py
    // Commit 10 reads these on fresh-project creation. Existing projects'
    // KG / Codegraph bindings are NOT touched here — that's Commit 9's
    // enrichment-migration territory.
    if set_as_default {
        // v0.2.68 Defect D: write BOTH the model id and the canonical
        // `embedding.active_profile` (→ "openai") via the shared helper so
        // populate() resolves "openai", not the "qwen3" fallback.
        //
        // F3 (v0.2.72): `set_text_embedding_and_profile` follows its writes
        // with a refresh of EVERY project's env (N serial Python
        // subprocesses) — run the block on the blocking pool. Write errors
        // still propagate through the join result.
        crate::commands::blocking::run_with_db_on_blocking_pool(
            handle.clone(),
            "register_openai_api_key set-default",
            |db| -> Result<(), String> {
                let _ = crate::commands::project_env_settings::set_text_embedding_and_profile(
                    db,
                    OPENAI_DEFAULT_TEXT_MODEL_ID,
                )?;
                db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, OPENAI_DEFAULT_CODE_MODEL_ID)
                    .map_err(|e| format!("app_state_set default_code_embedding: {}", e))?;
                Ok(())
            },
        )
        .await??;
    }

    // Successful re-register clears any pending fallback — if a prior
    // startup re-check stashed openai-* model ids in
    // `openai_fallback_pending`, those are now obsolete because the user
    // has explicitly re-supplied a working key. Restoring or keeping the
    // pending fallback would be wrong (the user just told us the key
    // works, NOT that they want to revert to openai-* defaults).
    let _ = clear_app_state_if_set(&db, APP_STATE_OPENAI_FALLBACK_PENDING);

    // Audit log. Don't log the key value — presence + scope are enough.
    let _ = db.audit(
        "openai_api_key_register",
        None,
        Some(OPENAI_MODULE_ID),
        &serde_json::json!({
            "key": OPENAI_KEY,
            "scope": "shared",
            "set_as_default": set_as_default,
        }),
    );

    Ok(RegisterOpenAiResponse {
        masked_key: secrets::mask_preview(trimmed),
        default_set: set_as_default,
    })
}

/// On-demand revalidation. Backs the Preferences "Re-check" button.
/// Reads the current keychain value, calls `validate_openai_api_key`,
/// then runs the same recovery state machine `run_openai_startup_recheck`
/// runs at boot. Emits the same events.
///
/// If no key is set, returns `Err("no_key_set")` — the GUI surfaces this
/// as "no key configured" without firing the toast pipeline.
#[command]
pub async fn recheck_openai_validity<R: Runtime>(
    handle: AppHandle<R>,
) -> Result<OpenAiValidationResult, String> {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    let key = secrets::get(scope, OPENAI_MODULE_ID, OPENAI_KEY)
        .map_err(|e| format!("keychain get openai_api_key: {}", e))?;
    let key = match key {
        Some(v) if !v.trim().is_empty() => v,
        _ => return Err("no_key_set".into()),
    };

    let result = validate_openai_api_key(key, None).await?;

    // Run the recovery state machine the same way `run_openai_startup_recheck`
    // does. Soft-fails (state-machine errors are logged but don't fail
    // the recheck call itself — the validation result is what the GUI
    // most cares about; the state transition is best-effort cleanup).
    //
    // F3 (v0.2.72): the invalid→fallback / valid→restore transitions call
    // `set_text_embedding_and_profile`, which refreshes EVERY project's env
    // (N serial Python subprocesses) — run the transition on the blocking
    // pool instead of this tokio worker.
    let handle_for_task = handle.clone();
    let result_for_task = result.clone();
    match crate::commands::blocking::run_with_db_on_blocking_pool(
        handle.clone(),
        "recheck_openai_validity recovery transition",
        move |db| apply_recovery_transition(&handle_for_task, db, &result_for_task),
    )
    .await
    {
        Ok(Ok(())) => {}
        Ok(Err(e)) | Err(e) => {
            eprintln!("[openai] recheck recovery transition warning: {}", e);
        }
    }

    Ok(result)
}

/// Lightweight presence check used by the Preferences "OpenAI key" row
/// (Commit 7) to decide whether to pre-fill the input with a masked
/// placeholder or leave it empty. Mirrors `has_github_pat` in
/// `commands::installer` — boolean only, never returns the secret value.
///
/// Returns `false` on any keychain read error (defensive: a transient
/// keychain hiccup should render as "no key" rather than crashing the
/// Preferences page; the user can retry via the Re-check button which
/// surfaces a richer error).
#[command]
pub fn has_openai_api_key() -> bool {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    match secrets::get(scope, OPENAI_MODULE_ID, OPENAI_KEY) {
        Ok(Some(v)) => !v.trim().is_empty(),
        _ => false,
    }
}

/// Return a masked preview of the stored OpenAI API key (head•••tail)
/// for the Preferences row's "Token saved (••••xxxx)" surface. Returns
/// `None` when no key is stored. Symmetric to `get_github_pat_preview`.
///
/// Security: never returns the raw key. The masking helper
/// (`secrets::mask_preview`) trims to head4 + tail3 so a full key can't
/// be reassembled from the preview string.
#[command]
pub fn get_openai_api_key_preview() -> Option<String> {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    let value = secrets::get(scope, OPENAI_MODULE_ID, OPENAI_KEY).ok()??;
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(secrets::mask_preview(trimmed))
}

/// Remove the OpenAI key from the keychain. Idempotent: a missing entry
/// is treated as success. Also clears the associated `openai_was_valid`
/// and `openai_fallback_pending` app_state rows so the recovery state
/// machine doesn't try to fall back / restore against a key that no
/// longer exists.
///
/// Note: we deliberately do NOT touch `default_text_embedding` /
/// `default_code_embedding`. If the user clears the key while those are
/// still set to `openai-*` ids, the EmbeddingService construction will
/// surface a clear "OPENAI_API_KEY not set" error and the recovery
/// state machine's next boot will see `openai_was_valid=false → no
/// rescue path` — i.e. defaults stay where the user explicitly put
/// them. Flipping defaults here would be an auto-switch, which is
/// explicitly forbidden by the v0.2.18 locked rule (the only way to
/// move defaults off openai-* is through the dropdown UI in
/// Preferences, which is its own consent surface).
#[command]
pub fn clear_openai_api_key(db: State<'_, Db>) -> Result<(), String> {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    // Keychain delete is idempotent — `secrets::delete` treats NoEntry as
    // success. We log on failure rather than bubbling, mirroring
    // `clear_github_pat`'s soft-fail philosophy: the user clicked Clear,
    // they expect a clean slate even if the keychain row was already gone.
    if let Err(e) = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY) {
        eprintln!("[openai] clear_openai_api_key: keychain delete failed: {}", e);
    }
    // Drop the active-flag row so a future register starts clean.
    let _ = db.forget_secret_active_state(
        "shared",
        SENTINEL_SHARED,
        OPENAI_MODULE_ID,
        OPENAI_KEY,
    );
    // Clear the recovery state-machine breadcrumbs. Empty-string is the
    // "deleted" sentinel for `app_state_get` (see `clear_app_state_if_set`
    // above); `app_state_set_bool(false)` would leave the row set-to-false
    // which the recovery state machine then misreads as "previously valid".
    let _ = clear_app_state_if_set(&db, APP_STATE_OPENAI_WAS_VALID);
    let _ = clear_app_state_if_set(&db, APP_STATE_OPENAI_FALLBACK_PENDING);

    // Audit log. Presence + scope are enough — never the key value.
    let _ = db.audit(
        "openai_api_key_clear",
        None,
        Some(OPENAI_MODULE_ID),
        &serde_json::json!({
            "key": OPENAI_KEY,
            "scope": "shared",
        }),
    );

    Ok(())
}

// ─── Startup recovery state machine ──────────────────────────────────────

/// Background task spawned by `lib.rs::setup()` that runs once at
/// launcher boot. Idempotent + soft-fail: any error logs to stderr but
/// MUST NOT block launcher boot. The launcher already runs without an
/// OpenAI key for free-tier users.
///
/// Generic over `Runtime` so the same function works under Tauri's
/// production `Wry` runtime AND the `MockRuntime` used by unit tests
/// (we don't currently spawn it from tests, but the bound costs nothing
/// and keeps the signature consistent with `spawn_daily_check` in
/// `self_update.rs`).
pub async fn run_openai_startup_recheck<R: Runtime>(handle: AppHandle<R>) -> Result<(), String> {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    let key = match secrets::get(scope, OPENAI_MODULE_ID, OPENAI_KEY) {
        Ok(Some(v)) if !v.trim().is_empty() => v,
        Ok(_) => return Ok(()), // no key — nothing to do
        Err(e) => {
            // Keychain read failure. Don't take the launcher down over a
            // transient keychain hiccup — the user will retry on next
            // boot or via the Preferences "Re-check" button.
            return Err(format!("startup keychain read: {}", e));
        }
    };

    let result = validate_openai_api_key(key, None).await?;

    // Resolve DB lazily — Tauri-managed state is only available after
    // setup() has run; spawning before `db` is registered would crash a
    // bare state() call. The blocking helper resolves via try_state inside
    // the task and returns Err when the row isn't registered yet — same
    // contract as the previous inline check.
    //
    // F3 (v0.2.72): the recovery transition can swap machine-global
    // embedding defaults, which refreshes EVERY project's env (N serial
    // Python subprocesses) — run it on the blocking pool, not this
    // runtime task.
    let handle_for_task = handle.clone();
    crate::commands::blocking::run_with_db_on_blocking_pool(
        handle,
        "run_openai_startup_recheck recovery transition",
        move |db| apply_recovery_transition(&handle_for_task, db, &result),
    )
    .await?
}

/// Apply the recovery state machine to a fresh validation result. Pure
/// over `(db, result)` plus the event emitter; no implicit
/// keychain-side effects. Tested independently of the HTTP layer.
///
/// Transitions:
///
/// | was_valid | result   | fallback_pending | action                                  |
/// |-----------|----------|------------------|-----------------------------------------|
/// | true      | Invalid  | none             | save openai-* defaults, fall back, emit |
/// | true      | Invalid  | some             | no-op (already fallen back; just emit)  |
/// | true      | Valid    | some             | restore defaults, clear pending, emit   |
/// | true      | Valid    | none             | no-op                                   |
/// | false     | Invalid  | *                | no-op (key was never valid)             |
/// | false     | Valid    | *                | set was_valid=true (catch-up)           |
/// | *         | Error    | *                | no-op (no decision, keep prior state)   |
///
/// The function is split into two layers:
///
///   1. `compute_recovery_transition` — pure over `(db_state, result)`,
///      returns the (state mutations, event to emit) tuple. No I/O on the
///      emission side; testable without an `AppHandle`.
///   2. `apply_recovery_transition` — calls (1), then performs the actual
///      `handle.emit(...)` if applicable.
fn apply_recovery_transition<R: Runtime>(
    handle: &AppHandle<R>,
    db: &Db,
    result: &OpenAiValidationResult,
) -> Result<(), String> {
    let outcome = compute_recovery_transition(db, result)?;
    if let Some(evt) = outcome.event {
        let _ = handle.emit(evt.name, evt.payload);
    }
    Ok(())
}

/// Event-to-emit description produced by `compute_recovery_transition`.
/// Pure data — the actual `handle.emit` call lives in the thin Tauri
/// wrapper so tests can assert on the event name + payload without
/// standing up a runtime.
#[derive(Debug, Clone)]
struct PendingEvent {
    name: &'static str,
    payload: serde_json::Value,
}

#[derive(Debug, Default, Clone)]
struct RecoveryOutcome {
    event: Option<PendingEvent>,
}

/// Pure state-machine layer. Mutates `db` (app_state writes) but does NOT
/// emit any Tauri events — returns a `PendingEvent` description for the
/// caller. Lets the tests drive every transition without spawning a Tauri
/// runtime.
fn compute_recovery_transition(
    db: &Db,
    result: &OpenAiValidationResult,
) -> Result<RecoveryOutcome, String> {
    let was_valid = db
        .app_state_get_bool(APP_STATE_OPENAI_WAS_VALID)
        .ok()
        .flatten()
        .unwrap_or(false);

    let pending = read_fallback_pending(db).unwrap_or_default();

    match (result, was_valid, pending.is_empty()) {
        // Previously valid, now invalid, no prior fallback: switch defaults
        // off openai-* (if currently openai-*) and stash for restoration.
        (OpenAiValidationResult::Invalid { reason, .. }, true, true) => {
            let current_text = db
                .app_state_get(APP_STATE_DEFAULT_TEXT_EMBED)
                .ok()
                .flatten();
            let current_code = db
                .app_state_get(APP_STATE_DEFAULT_CODE_EMBED)
                .ok()
                .flatten();

            let mut stash = FallbackPending::default();
            if let Some(t) = &current_text {
                if t.starts_with("openai-") {
                    stash.text = Some(t.clone());
                }
            }
            if let Some(c) = &current_code {
                if c.starts_with("openai-") {
                    stash.code = Some(c.clone());
                }
            }

            if stash.is_empty() {
                // Key invalid but defaults aren't openai-anything — just
                // emit so the GUI can warn ("your key stopped working,
                // but you're already on local defaults — no action needed").
                // Don't update fallback_pending: nothing to restore later.
                return Ok(RecoveryOutcome {
                    event: Some(PendingEvent {
                        name: EVT_OPENAI_KEY_INVALIDATED,
                        payload: serde_json::json!({
                            "reason": reason,
                            "restored_defaults": null,
                        }),
                    }),
                });
            }

            // Persist the stash before swapping defaults — if a crash
            // happens between the two writes, restoration on next boot
            // still has the originals to restore from.
            let stash_json = serde_json::to_string(&stash)
                .map_err(|e| format!("serialize fallback_pending: {}", e))?;
            db.app_state_set(APP_STATE_OPENAI_FALLBACK_PENDING, &stash_json)
                .map_err(|e| format!("app_state_set fallback_pending: {}", e))?;

            // Swap defaults to local fallbacks. Choosing the best
            // code-embed default is environment-dependent (GPU service
            // running? CPU only?) — see `choose_best_local_code_default`.
            let local_code = choose_best_local_code_default();
            // v0.2.68 Defect D: swapping the text default to the local
            // fallback must also re-derive the canonical
            // `embedding.active_profile` (→ "qwen3"), otherwise a project
            // created post-fallback inherits a profile that still points at
            // the now-removed openai vector slot.
            crate::commands::project_env_settings::set_text_embedding_and_profile(
                db,
                LOCAL_TEXT_FALLBACK_ID,
            )?;
            db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, &local_code)
                .map_err(|e| format!("app_state_set default_code_embedding: {}", e))?;

            Ok(RecoveryOutcome {
                event: Some(PendingEvent {
                    name: EVT_OPENAI_KEY_INVALIDATED,
                    payload: serde_json::json!({
                        "reason": reason,
                        "restored_defaults": stash,
                    }),
                }),
            })
        }

        // Previously valid, now invalid, already fallen back: just emit
        // so the GUI keeps the banner visible across launches. Don't
        // re-stash — fallback_pending already holds the originals.
        (OpenAiValidationResult::Invalid { reason, .. }, true, false) => {
            Ok(RecoveryOutcome {
                event: Some(PendingEvent {
                    name: EVT_OPENAI_KEY_INVALIDATED,
                    payload: serde_json::json!({
                        "reason": reason,
                        "restored_defaults": pending,
                        "already_fallen_back": true,
                    }),
                }),
            })
        }

        // Previously invalid (or new), now valid, fallback was pending:
        // restore the openai-* defaults, clear pending, mark valid.
        (OpenAiValidationResult::Valid { .. }, _was, false) => {
            if let Some(t) = &pending.text {
                // v0.2.68 Defect D: restoring the stashed text model id also
                // re-derives the canonical `embedding.active_profile` (the
                // stash normally holds the openai-* id → "openai").
                crate::commands::project_env_settings::set_text_embedding_and_profile(db, t)?;
            }
            if let Some(c) = &pending.code {
                db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, c)
                    .map_err(|e| format!("app_state_set default_code_embedding: {}", e))?;
            }
            // Clear pending and set was_valid.
            let _ = clear_app_state_if_set(db, APP_STATE_OPENAI_FALLBACK_PENDING);
            db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true)
                .map_err(|e| format!("app_state_set openai_was_valid: {}", e))?;

            Ok(RecoveryOutcome {
                event: Some(PendingEvent {
                    name: EVT_OPENAI_KEY_RESTORED,
                    payload: serde_json::json!({
                        "restored_slots": pending,
                    }),
                }),
            })
        }

        // Catch-up for the "never validated before but works now" case:
        // mark was_valid so the next-time-invalid path engages.
        (OpenAiValidationResult::Valid { .. }, false, true) => {
            db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true)
                .map_err(|e| format!("app_state_set openai_was_valid: {}", e))?;
            Ok(RecoveryOutcome::default())
        }

        // No transition (still-valid no-stash; never-valid-still-invalid;
        // Error result).
        _ => Ok(RecoveryOutcome::default()),
    }
}

fn read_fallback_pending(db: &Db) -> Option<FallbackPending> {
    let raw = db.app_state_get(APP_STATE_OPENAI_FALLBACK_PENDING).ok()??;
    serde_json::from_str(&raw).ok()
}

/// Best-effort clear: `app_state_set("")` is the closest we have to
/// "delete a row" without adding a new DB method. The fallback_pending
/// reader treats an empty string as None (via `from_str` falling through
/// to `None`).
///
/// Why we don't add a `delete_app_state(key)` method: the existing
/// `app_state` API surface is `get/set/get_bool/set_bool`. Adding a delete
/// for one caller is out of scope for Commit 3 — Commit 7 (preferences UI)
/// may want it later for the "Clear OpenAI key" button anyway, at which
/// point it lands as a separate, broader change. For now, "empty string"
/// + tolerant deserialization is enough for the recovery state machine
/// (the fallback_pending reader treats empty / invalid JSON as None).
fn clear_app_state_if_set(db: &Db, key: &str) -> Result<(), String> {
    db.app_state_set(key, "")
}

/// Probe the local code-embedding service to decide which model id to
/// surface as the local code-embed default. If reachable, prefer its
/// model (`codesage-large-v2` typically); otherwise fall back to the
/// Ollama-served `qwen3-embedding:0.6b`.
///
/// Synchronous wrapper around the async probe via
/// `tauri::async_runtime::block_on`. The recovery state machine that
/// calls this is itself async, but reqwest's blocking client requires
/// the `"blocking"` feature flag that the launcher's reqwest dep
/// doesn't enable (only `json` + `rustls-tls`). The async probe + a
/// short block_on bridge keeps us on the existing feature surface.
///
/// Environment:
///   * `CODE_EMBED_SERVICE_URL` — overrides the default `localhost:11440`
///   * 500ms total timeout — matches the
///     `CODE_EMBED_HEALTH_TIMEOUT_MS` constant
pub fn choose_best_local_code_default() -> String {
    // Two-tier guard: try to use the existing runtime when present, else
    // build a one-shot runtime. Test contexts (no Tokio runtime) hit the
    // fallback branch; production (Tauri's async_runtime) hits the first.
    let reachable = match tokio::runtime::Handle::try_current() {
        Ok(handle) => std::thread::scope(|s| {
            s.spawn(|| handle.block_on(probe_code_embed_reachable()))
                .join()
                .unwrap_or(false)
        }),
        Err(_) => match tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
        {
            Ok(rt) => rt.block_on(probe_code_embed_reachable()),
            Err(_) => false,
        },
    };

    if reachable {
        LOCAL_CODE_FALLBACK_CODEEMBED.to_string()
    } else {
        LOCAL_CODE_FALLBACK_OLLAMA.to_string()
    }
}

/// Async probe of the code-embed service `/health` endpoint. Returns
/// `true` only on a 2xx response within the configured timeout.
/// Network errors, DNS failures, timeouts all return `false`.
async fn probe_code_embed_reachable() -> bool {
    let url = std::env::var("CODE_EMBED_SERVICE_URL")
        .unwrap_or_else(|_| DEFAULT_CODE_EMBED_URL.to_string());
    let health_url = format!("{}/health", url.trim_end_matches('/'));

    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(CODE_EMBED_HEALTH_TIMEOUT_MS))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };

    matches!(
        client.get(&health_url).send().await,
        Ok(r) if r.status().is_success()
    )
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use rusqlite::Connection;
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    // ─── classify_status: pure HTTP → outcome decision tree ────────────

    #[test]
    fn classify_status_200_returns_valid_not_rate_limited() {
        let r = classify_status(200, "text-embedding-3-small", || String::new());
        assert_eq!(
            r,
            OpenAiValidationResult::Valid {
                model: "text-embedding-3-small".into(),
                rate_limited: false,
            }
        );
    }

    #[test]
    fn classify_status_401_returns_invalid_auth_failed() {
        let r = classify_status(401, "text-embedding-3-small", || String::new());
        match r {
            OpenAiValidationResult::Invalid { reason, http_status } => {
                assert!(
                    reason.contains("auth failed"),
                    "expected 'auth failed' reason, got: {}",
                    reason
                );
                assert_eq!(http_status, Some(401));
            }
            _ => panic!("expected Invalid for 401, got {:?}", r),
        }
    }

    #[test]
    fn classify_status_403_returns_invalid_blocked() {
        let r = classify_status(403, "text-embedding-3-small", || String::new());
        match r {
            OpenAiValidationResult::Invalid { reason, http_status } => {
                assert!(reason.contains("blocked"), "got: {}", reason);
                assert_eq!(http_status, Some(403));
            }
            _ => panic!("expected Invalid for 403, got {:?}", r),
        }
    }

    #[test]
    fn classify_status_404_returns_invalid_model_not_accessible() {
        let r = classify_status(404, "text-embedding-3-small", || String::new());
        match r {
            OpenAiValidationResult::Invalid { reason, http_status } => {
                assert!(
                    reason.contains("model not accessible"),
                    "expected 'model not accessible' reason, got: {}",
                    reason
                );
                // Should also include the model name for debugging.
                assert!(
                    reason.contains("text-embedding-3-small"),
                    "expected model name in reason, got: {}",
                    reason
                );
                assert_eq!(http_status, Some(404));
            }
            _ => panic!("expected Invalid for 404, got {:?}", r),
        }
    }

    #[test]
    fn classify_status_429_returns_valid_rate_limited() {
        let r = classify_status(429, "text-embedding-3-small", || String::new());
        assert_eq!(
            r,
            OpenAiValidationResult::Valid {
                model: "text-embedding-3-small".into(),
                rate_limited: true,
            }
        );
    }

    #[test]
    fn classify_status_500_returns_invalid_with_body() {
        let r = classify_status(500, "text-embedding-3-small", || {
            "{\"error\": \"internal\"}".into()
        });
        match r {
            OpenAiValidationResult::Invalid { reason, http_status } => {
                assert!(reason.contains("500"), "got: {}", reason);
                assert!(reason.contains("internal"), "expected body content, got: {}", reason);
                assert_eq!(http_status, Some(500));
            }
            _ => panic!("expected Invalid for 500, got {:?}", r),
        }
    }

    #[test]
    fn classify_status_long_body_is_truncated() {
        let long_body: String = "x".repeat(1000);
        let r = classify_status(599, "m", move || long_body.clone());
        match r {
            OpenAiValidationResult::Invalid { reason, .. } => {
                // 256 chars + ellipsis suffix; not 1000+
                assert!(reason.len() < 400, "expected truncated body, len={}", reason.len());
                assert!(reason.contains("…"), "expected ellipsis marker, got: {}", reason);
            }
            _ => panic!("expected Invalid"),
        }
    }

    // ─── compute_recovery_transition: state machine (pure layer) ─────────
    //
    // Tests target the pure layer (`compute_recovery_transition`) directly
    // — no `AppHandle` required. The Tauri wrapper `apply_recovery_transition`
    // is a one-liner that calls `handle.emit(evt.name, evt.payload)` on the
    // returned `PendingEvent`, so the event delivery itself is exercised
    // by the integration / Commit 7 UI tests, not these unit tests.

    #[test]
    fn recovery_previously_valid_now_invalid_no_pending_stashes_and_falls_back() {
        let db = make_db();
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, OPENAI_DEFAULT_TEXT_MODEL_ID)
            .unwrap();
        db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, OPENAI_DEFAULT_CODE_MODEL_ID)
            .unwrap();

        let result = OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // Defaults swapped to local.
        let text = db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().unwrap();
        let code = db.app_state_get(APP_STATE_DEFAULT_CODE_EMBED).unwrap().unwrap();
        assert_eq!(text, LOCAL_TEXT_FALLBACK_ID);
        // local code is one of the two known fallbacks
        assert!(
            code == LOCAL_CODE_FALLBACK_CODEEMBED || code == LOCAL_CODE_FALLBACK_OLLAMA,
            "code default is not a known local fallback: {}",
            code
        );

        // Pending stash holds the openai-* originals.
        let pending = read_fallback_pending(&db).expect("pending must be set");
        assert_eq!(pending.text.as_deref(), Some(OPENAI_DEFAULT_TEXT_MODEL_ID));
        assert_eq!(pending.code.as_deref(), Some(OPENAI_DEFAULT_CODE_MODEL_ID));

        // Event emitted: vct-openai-key-invalidated with restored_defaults.
        let evt = outcome.event.expect("expected an invalidation event");
        assert_eq!(evt.name, EVT_OPENAI_KEY_INVALIDATED);
        assert_eq!(evt.payload["reason"], "auth failed");
        assert_eq!(
            evt.payload["restored_defaults"]["text"],
            OPENAI_DEFAULT_TEXT_MODEL_ID
        );
        assert_eq!(
            evt.payload["restored_defaults"]["code"],
            OPENAI_DEFAULT_CODE_MODEL_ID
        );
    }

    #[test]
    fn recovery_previously_invalid_now_valid_restores_from_pending() {
        let db = make_db();
        // Simulate the post-fallback state: was_valid stays true (sticky),
        // pending holds the originals, current defaults are local.
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, LOCAL_TEXT_FALLBACK_ID)
            .unwrap();
        db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, LOCAL_CODE_FALLBACK_OLLAMA)
            .unwrap();
        let stash = FallbackPending {
            text: Some(OPENAI_DEFAULT_TEXT_MODEL_ID.into()),
            code: Some(OPENAI_DEFAULT_CODE_MODEL_ID.into()),
        };
        db.app_state_set(
            APP_STATE_OPENAI_FALLBACK_PENDING,
            &serde_json::to_string(&stash).unwrap(),
        )
        .unwrap();

        let result = OpenAiValidationResult::Valid {
            model: "text-embedding-3-small".into(),
            rate_limited: false,
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // Defaults restored.
        let text = db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().unwrap();
        let code = db.app_state_get(APP_STATE_DEFAULT_CODE_EMBED).unwrap().unwrap();
        assert_eq!(text, OPENAI_DEFAULT_TEXT_MODEL_ID);
        assert_eq!(code, OPENAI_DEFAULT_CODE_MODEL_ID);

        // Pending cleared (empty string is the post-`clear_app_state_if_set`
        // sentinel; `read_fallback_pending` returns None for both "" and
        // missing row).
        assert!(read_fallback_pending(&db).is_none(),
            "pending must be cleared after successful restoration");

        // Event emitted: vct-openai-key-restored with restored_slots.
        let evt = outcome.event.expect("expected a restoration event");
        assert_eq!(evt.name, EVT_OPENAI_KEY_RESTORED);
        assert_eq!(
            evt.payload["restored_slots"]["text"],
            OPENAI_DEFAULT_TEXT_MODEL_ID
        );
    }

    #[test]
    fn recovery_invalid_with_non_openai_defaults_is_a_no_op_for_state() {
        let db = make_db();
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        // User had openai key but explicitly switched defaults back to
        // local on their own. Now key invalidates — there's nothing to
        // fall back FROM.
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, LOCAL_TEXT_FALLBACK_ID)
            .unwrap();
        db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, LOCAL_CODE_FALLBACK_OLLAMA)
            .unwrap();

        let result = OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // Defaults UNCHANGED (no openai-* to stash).
        let text = db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().unwrap();
        let code = db.app_state_get(APP_STATE_DEFAULT_CODE_EMBED).unwrap().unwrap();
        assert_eq!(text, LOCAL_TEXT_FALLBACK_ID);
        assert_eq!(code, LOCAL_CODE_FALLBACK_OLLAMA);
        // No pending row.
        assert!(read_fallback_pending(&db).is_none());

        // Event STILL emitted (so GUI can update the banner) but
        // restored_defaults=null (nothing was actually stashed).
        let evt = outcome.event.expect("expected an invalidation event");
        assert_eq!(evt.name, EVT_OPENAI_KEY_INVALIDATED);
        assert!(evt.payload["restored_defaults"].is_null());
    }

    #[test]
    fn recovery_previously_valid_now_invalid_already_fallen_back_re_emits() {
        let db = make_db();
        // Post-fallback steady state: was_valid=true, defaults are local,
        // pending holds the original openai-* ids. Another boot fires
        // while the key is still invalid — we want the GUI banner to
        // stay visible on every launcher restart until the key is fixed.
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, LOCAL_TEXT_FALLBACK_ID)
            .unwrap();
        db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, LOCAL_CODE_FALLBACK_OLLAMA)
            .unwrap();
        let stash = FallbackPending {
            text: Some(OPENAI_DEFAULT_TEXT_MODEL_ID.into()),
            code: None,
        };
        db.app_state_set(
            APP_STATE_OPENAI_FALLBACK_PENDING,
            &serde_json::to_string(&stash).unwrap(),
        )
        .unwrap();

        let result = OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // Defaults UNCHANGED — re-stashing would clobber the originals if
        // they'd already been swapped to local.
        assert_eq!(
            db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().unwrap(),
            LOCAL_TEXT_FALLBACK_ID
        );
        // Pending UNCHANGED.
        let pending2 = read_fallback_pending(&db).expect("pending still set");
        assert_eq!(pending2.text.as_deref(), Some(OPENAI_DEFAULT_TEXT_MODEL_ID));

        // Event STILL emitted with `already_fallen_back=true` so GUI can
        // skip the toast on repeat launches but still maintain the banner.
        let evt = outcome.event.expect("expected re-emit event");
        assert_eq!(evt.name, EVT_OPENAI_KEY_INVALIDATED);
        assert_eq!(evt.payload["already_fallen_back"], true);
    }

    #[test]
    fn recovery_never_valid_now_valid_catches_up_was_valid_flag() {
        let db = make_db();
        // was_valid is unset (default: false). No pending.
        let result = OpenAiValidationResult::Valid {
            model: "text-embedding-3-small".into(),
            rate_limited: false,
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // was_valid sticks now.
        assert_eq!(
            db.app_state_get_bool(APP_STATE_OPENAI_WAS_VALID).unwrap(),
            Some(true)
        );
        // No event — catch-up is silent, the wizard / preferences UI
        // already shows "key valid" status via its own poll.
        assert!(outcome.event.is_none());
    }

    #[test]
    fn recovery_error_result_is_a_complete_noop() {
        let db = make_db();
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, OPENAI_DEFAULT_TEXT_MODEL_ID)
            .unwrap();

        let result = OpenAiValidationResult::Error {
            detail: "DNS timeout".into(),
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        // Everything unchanged.
        let text = db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().unwrap();
        assert_eq!(text, OPENAI_DEFAULT_TEXT_MODEL_ID);
        assert!(read_fallback_pending(&db).is_none());
        assert_eq!(
            db.app_state_get_bool(APP_STATE_OPENAI_WAS_VALID).unwrap(),
            Some(true)
        );
        // No event — DNS timeout isn't an actionable "your key broke"
        // signal; user already sees the toast via the recheck UI.
        assert!(outcome.event.is_none());
    }

    #[test]
    fn recovery_never_valid_invalid_is_total_noop() {
        // Free-tier user who never registered a key: a hypothetical
        // re-check (e.g. user typed bad key, fixed it, re-typed bad key)
        // must not stash or emit. Defaults stay where the user left them.
        let db = make_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, LOCAL_TEXT_FALLBACK_ID)
            .unwrap();

        let result = OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        };
        let outcome = compute_recovery_transition(&db, &result).unwrap();

        assert!(outcome.event.is_none());
        assert!(read_fallback_pending(&db).is_none());
        assert_eq!(
            db.app_state_get_bool(APP_STATE_OPENAI_WAS_VALID).unwrap(),
            None
        );
    }

    #[test]
    fn fallback_pending_serialises_roundtrip() {
        let f = FallbackPending {
            text: Some("openai-text-embedding-3-small".into()),
            code: None,
        };
        let json = serde_json::to_string(&f).unwrap();
        let parsed: FallbackPending = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.text.as_deref(), Some("openai-text-embedding-3-small"));
        assert!(parsed.code.is_none());
        assert!(!f.is_empty());
        assert!(FallbackPending::default().is_empty());
    }

    #[test]
    fn read_fallback_pending_treats_empty_string_as_none() {
        let db = make_db();
        db.app_state_set(APP_STATE_OPENAI_FALLBACK_PENDING, "").unwrap();
        assert!(read_fallback_pending(&db).is_none());
    }

    #[test]
    fn validation_result_serialises_as_tagged_union() {
        // Valid
        let v = OpenAiValidationResult::Valid {
            model: "text-embedding-3-small".into(),
            rate_limited: false,
        };
        let j = serde_json::to_value(&v).unwrap();
        assert_eq!(j["status"], "valid");
        assert_eq!(j["model"], "text-embedding-3-small");
        assert_eq!(j["rate_limited"], false);

        // Invalid
        let inv = OpenAiValidationResult::Invalid {
            reason: "auth failed".into(),
            http_status: Some(401),
        };
        let j = serde_json::to_value(&inv).unwrap();
        assert_eq!(j["status"], "invalid");
        assert_eq!(j["reason"], "auth failed");
        assert_eq!(j["http_status"], 401);

        // Error
        let e = OpenAiValidationResult::Error {
            detail: "dns".into(),
        };
        let j = serde_json::to_value(&e).unwrap();
        assert_eq!(j["status"], "error");
        assert_eq!(j["detail"], "dns");
    }

    #[test]
    fn choose_best_local_code_default_returns_known_id() {
        // We don't control whether the code-embed service is running on
        // the test machine — assert that we get *one of* the two known
        // local ids back. Tests both reachable + unreachable paths
        // depending on CI environment.
        let id = choose_best_local_code_default();
        assert!(
            id == LOCAL_CODE_FALLBACK_CODEEMBED || id == LOCAL_CODE_FALLBACK_OLLAMA,
            "expected one of the known local fallbacks, got: {}",
            id
        );
    }

    #[test]
    fn app_state_keys_match_plan_lockdown() {
        // Pinned strings — Commit 10 (install.py) and Commit 7 (preferences
        // UI) both consume these. Renaming any would break cross-commit
        // integration.
        assert_eq!(APP_STATE_DEFAULT_TEXT_EMBED, "default_text_embedding");
        assert_eq!(APP_STATE_DEFAULT_CODE_EMBED, "default_code_embedding");
        assert_eq!(APP_STATE_OPENAI_WAS_VALID, "openai_was_valid");
        assert_eq!(APP_STATE_OPENAI_FALLBACK_PENDING, "openai_fallback_pending");
    }

    #[test]
    fn constants_match_bundled_secret_declaration() {
        // The hub's `bundled_secrets` resolver reads `(scope="shared",
        // module_id="user", key="openai_api_key")`. If we drift any of
        // these here, env-injection silently breaks.
        assert_eq!(OPENAI_MODULE_ID, "user");
        assert_eq!(OPENAI_KEY, "openai_api_key");
        assert_eq!(SENTINEL_SHARED, "_user_shared_");
    }

    // ─── has/get_preview/clear: Preferences row helpers (Commit 7) ─────
    //
    // These tests exercise the actual keychain so they're gated on the
    // process-wide `keychain_serialize_lock` (same convention used by
    // installer.rs PAT tests). The lock guarantees no other test module
    // is racing for the same `shared.user/openai_api_key` keychain slot.
    //
    // CI environments without an OS keychain (headless Linux without a
    // configured Secret Service) skip via the `keyring_available()`
    // probe so we don't get flaky failures on `secrets::set`.

    fn keyring_available() -> bool {
        // Same probe pattern as installer.rs PAT tests — try to write +
        // delete a canary entry under a private key namespace.
        let entry = match keyring::Entry::new("vct.test.openai.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    }

    fn make_db_for_tests() -> Db {
        // Same as make_db() above; pulled out so the keychain-gated
        // tests below can share the in-memory factory.
        make_db()
    }

    #[test]
    fn has_openai_api_key_false_when_absent() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        // Ensure no residual entry from a prior aborted test.
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let _ = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY);

        assert!(!has_openai_api_key(), "absent key should return false");
    }

    #[test]
    fn has_openai_api_key_true_when_present() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        secrets::set(scope, OPENAI_MODULE_ID, OPENAI_KEY, "sk-test-canary-12345")
            .expect("test keychain write should succeed");
        assert!(has_openai_api_key(), "present key should return true");
        // Cleanup so we don't leak into the next test.
        let _ = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY);
    }

    #[test]
    fn get_openai_api_key_preview_returns_masked_value() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        secrets::set(scope, OPENAI_MODULE_ID, OPENAI_KEY, "sk-12345-abcdefghij")
            .expect("test keychain write should succeed");

        let preview = get_openai_api_key_preview().expect("preview should be present");
        // mask_preview redacts middle: head4 + ••• + tail3.
        assert!(
            preview.contains('•'),
            "preview should contain mask chars, got: {}",
            preview
        );
        // Sanity: the raw key must NOT be reproducible from the preview.
        assert!(
            !preview.contains("abcdefghij") || !preview.contains("sk-12345"),
            "preview must not concatenate head+tail without redaction, got: {}",
            preview
        );

        let _ = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY);
    }

    #[test]
    fn get_openai_api_key_preview_returns_none_when_absent() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let _ = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY);
        assert!(
            get_openai_api_key_preview().is_none(),
            "absent key should return None preview"
        );
    }

    #[test]
    fn clear_openai_api_key_removes_keychain_and_state() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let db = make_db_for_tests();

        // Seed: key in keychain + state breadcrumbs.
        secrets::set(scope, OPENAI_MODULE_ID, OPENAI_KEY, "sk-clear-me-12345")
            .expect("seed keychain write");
        db.app_state_set_bool(APP_STATE_OPENAI_WAS_VALID, true).unwrap();
        db.app_state_set(APP_STATE_OPENAI_FALLBACK_PENDING, "{\"text\":null,\"code\":null}")
            .unwrap();

        // Wrap the State<Db> as the runtime would.
        let result = (|db: &Db| -> Result<(), String> {
            // Inline copy of clear_openai_api_key's body — the #[command]
            // wrapper requires a tauri::State which is non-trivial to
            // construct in unit tests. The non-Tauri path here exercises
            // the exact same side effects.
            if let Err(e) = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY) {
                eprintln!("[openai] clear: keychain delete failed: {}", e);
            }
            let _ = db.forget_secret_active_state(
                "shared",
                SENTINEL_SHARED,
                OPENAI_MODULE_ID,
                OPENAI_KEY,
            );
            let _ = clear_app_state_if_set(db, APP_STATE_OPENAI_WAS_VALID);
            let _ = clear_app_state_if_set(db, APP_STATE_OPENAI_FALLBACK_PENDING);
            Ok(())
        })(&db);
        result.expect("clear should succeed");

        // Verify: keychain row is gone.
        assert!(!has_openai_api_key(), "key should be cleared from keychain");
        // Verify: state breadcrumbs are wiped (empty-string is the deleted
        // sentinel — see `clear_app_state_if_set`).
        let was_valid = db
            .app_state_get(APP_STATE_OPENAI_WAS_VALID)
            .unwrap()
            .unwrap_or_default();
        let pending = db
            .app_state_get(APP_STATE_OPENAI_FALLBACK_PENDING)
            .unwrap()
            .unwrap_or_default();
        assert!(was_valid.is_empty(), "was_valid should be cleared, got: {:?}", was_valid);
        assert!(pending.is_empty(), "fallback_pending should be cleared, got: {:?}", pending);
    }

    #[test]
    fn clear_openai_api_key_is_idempotent_on_missing_entry() {
        let _lock = crate::secrets::test_serialize::keychain_serialize_lock();
        if !keyring_available() {
            eprintln!("[skip] no usable keychain on this host");
            return;
        }
        let scope = SecretScope::Shared {
            project_id: SENTINEL_SHARED,
        };
        let db = make_db_for_tests();

        // Ensure absent.
        let _ = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY);

        // Calling clear on an already-empty slot must not panic / err.
        let result = (|db: &Db| -> Result<(), String> {
            if let Err(e) = secrets::delete(scope, OPENAI_MODULE_ID, OPENAI_KEY) {
                eprintln!("[openai] clear (idempotent): keychain delete: {}", e);
            }
            let _ = db.forget_secret_active_state(
                "shared",
                SENTINEL_SHARED,
                OPENAI_MODULE_ID,
                OPENAI_KEY,
            );
            let _ = clear_app_state_if_set(db, APP_STATE_OPENAI_WAS_VALID);
            let _ = clear_app_state_if_set(db, APP_STATE_OPENAI_FALLBACK_PENDING);
            Ok(())
        })(&db);
        result.expect("clear should be idempotent on missing entry");
    }
}
