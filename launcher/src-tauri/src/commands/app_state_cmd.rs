//! Tauri commands exposing the launcher.db `app_state` key-value table
//! to the frontend. These replace direct localStorage reads/writes for
//! launcher-state flags that need to be isolated by VCT_STATE_DIR
//! (Bug 14 fix).
//!
//! Naming convention for keys: dotted, lowercase, ecosystem-prefixed.
//! Frontend should use stable constants, e.g.:
//!     const KEY_ONBOARDING_COMPLETE = "onboarding.complete";
//!     const KEY_TELEMETRY_TERMS_ACCEPTED = "telemetry.terms_accepted";
//!
//! `null` (None on the Rust side, `null` on the JS side) means "no row
//! exists" — i.e. apply default behaviour. Callers that need to
//! distinguish "user explicitly set to false" from "never set" MUST
//! check for null vs false.

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;

/// Result envelope for `get_app_state` so the frontend can reliably
/// distinguish "row absent" from "row present with empty value". A
/// plain `Option<String>` would also work but JSON-serialised `null`
/// is easy to misuse on the JS side; explicit `is_set` removes ambiguity.
#[derive(Debug, Serialize)]
pub struct AppStateGetResult {
    pub key: String,
    pub is_set: bool,
    pub value: Option<String>,
}

#[command]
pub async fn app_state_get(
    key: String,
    db: State<'_, Db>,
) -> Result<AppStateGetResult, String> {
    let raw = db.app_state_get(&key)?;
    Ok(AppStateGetResult {
        key,
        is_set: raw.is_some(),
        value: raw,
    })
}

#[command]
pub async fn app_state_set(
    key: String,
    value: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.app_state_set(&key, &value)
}

/// Boolean convenience. Returns `null` when the row is absent (so the
/// frontend can apply default behaviour), `true`/`false` otherwise.
#[command]
pub async fn app_state_get_bool(
    key: String,
    db: State<'_, Db>,
) -> Result<Option<bool>, String> {
    db.app_state_get_bool(&key)
}

#[command]
pub async fn app_state_set_bool(
    key: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.app_state_set_bool(&key, value)
}
