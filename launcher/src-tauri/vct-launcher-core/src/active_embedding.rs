// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! The machine-global ACTIVE_EMBEDDING profile — the Rust home shared by the
//! hub (`config_api`), the launcher (`project_env_settings`) and the RL
//! scoring default (`rl_scoring_default`, which needs the active profile to
//! know whether a trained model is bundled for it).
//!
//! Until v0.2.106 the hub and the launcher each carried a copy of this
//! resolution (the hub crate could not reach the launcher crate's). It moved
//! here when a third Rust caller appeared.
//!
//! Order (must match the Python `config_projection.py::
//! _resolve_active_embedding_cascade`):
//!
//!   1. `app_state[embedding.active_profile]` (Preferences → Embedding
//!      profile), then the hardware-pick derive
//!      (`app_state[default_text_embedding]` mapped by
//!      [`active_profile_for_model`]);
//!   2. `"qwen3"`.
//!
//! Soft-fail: every DB read is best-effort.

use crate::db::Db;

/// `app_state` key of the ONE global embedding profile.
pub const APP_STATE_KEY_ACTIVE_EMBEDDING: &str = "embedding.active_profile";
/// `app_state` key of the hardware-picked default TEXT embedding model id.
pub const APP_STATE_KEY_DEFAULT_TEXT_EMBEDDING: &str = "default_text_embedding";
/// Final fallback profile.
pub const DEFAULT_ACTIVE_EMBEDDING: &str = "qwen3";

/// TEXT model id → ACTIVE_EMBEDDING profile.
///
/// MUST match (drift re-introduces v0.2.68 Defect D):
///   * `vco_lib/launcher_db_reader.py::_TEXT_MODEL_ACTIVE_EMBEDDING`
///   * `install.py::_TEXT_MODEL_ACTIVE_EMBEDDING`
pub fn active_profile_for_model(model_id: &str) -> Option<&'static str> {
    match model_id.trim() {
        "qwen3-embedding:0.6b" => Some("qwen3"),
        "snowflake-arctic-embed2:latest" => Some("arctic"),
        "openai-text-embedding-3-small" => Some("openai"),
        "text-embedding-3-small" => Some("openai"),
        _ => None,
    }
}

/// The machine-global leg: the profile key, else the mapped hardware pick.
pub fn global_active_embedding(db: &Db) -> Option<String> {
    db.app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            db.app_state_get(APP_STATE_KEY_DEFAULT_TEXT_EMBEDDING)
                .ok()
                .flatten()
                .and_then(|model_id| active_profile_for_model(&model_id))
                .map(|p| p.to_string())
        })
}

/// The ONE active-embedding resolution (one global profile for every project).
pub fn resolve_active_embedding(db: &Db) -> String {
    global_active_embedding(db).unwrap_or_else(|| DEFAULT_ACTIVE_EMBEDDING.to_string())
}
