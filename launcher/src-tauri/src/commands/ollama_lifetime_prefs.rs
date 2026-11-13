// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106 (owner ruling 2026-11-11) — the GUI side of "the hub manages how
//! long Ollama keeps a model loaded".
//!
//! * Preferences → **Ollama model unloading**: one machine-wide idle time in
//!   `app_state[ollama.idle_unload]`. The hub
//!   (`vct_hub::ollama_lifetime`) reads it on every check (every 5–30 s), so
//!   a change takes effect on the hub's next check — no project refresh, no
//!   MCP restart: VCO's callers send the same keep_alive whatever the setting.
//! * Services → **Ollama models**: the hub's list of loaded models (size,
//!   RAM/VRAM, who holds each one, when the hub will unload it) and its
//!   "Unload now" route. Both go through the hub, never to Ollama directly,
//!   so the list and the unloader share one tracker.
//!
//! The parser, the bounds and the key live in
//! `vct_launcher_core::services::ollama_lifetime` — the hub resolves with the
//! same code, so a value this command accepts is one the hub honours.

use serde::Serialize;
use serde_json::Value;
use tauri::{command, State};

use crate::db::Db;
use vct_launcher_core::services::ollama_lifetime as lt;

/// What the Preferences field shows.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct OllamaIdleState {
    /// The stored value (`None` = no row: the default applies).
    pub stored: Option<String>,
    /// The idle time the hub uses, e.g. `"10m"` (`"0"` = as soon as idle).
    pub effective: String,
    pub effective_secs: u64,
    /// A stored value the hub cannot honour (hand-edited row): the default
    /// applies and the GUI says so.
    pub stored_invalid: bool,
    pub default_idle: String,
    pub max_idle: String,
    /// The keep_alive VCO's callers send — how long a model stays loaded on
    /// its own if the hub is not running.
    pub caller_keep_alive: String,
    /// `VCO_OLLAMA_KEEP_ALIVE` as this launcher's environment holds it
    /// (`None` when unset). Processes that inherit it send that keep_alive
    /// instead, and the hub leaves those models to Ollama's own timer.
    pub env_override: Option<String>,
}

fn state_from(db: &Db, env_override: Option<String>) -> OllamaIdleState {
    let idle = lt::idle_from_db(db);
    let table = lt::table();
    OllamaIdleState {
        stored: idle.stored.clone(),
        effective: lt::format_secs(idle.secs),
        effective_secs: idle.secs,
        stored_invalid: idle.stored_invalid,
        default_idle: lt::format_secs(table.default_idle_secs),
        max_idle: lt::format_secs(table.max_idle_secs),
        caller_keep_alive: lt::format_secs(table.caller_keep_alive_secs),
        env_override,
    }
}

fn launcher_env_override() -> Option<String> {
    std::env::var(lt::CALLER_OVERRIDE_ENV).ok()
}

/// Free-function core of [`get_ollama_idle_unload`].
pub fn ollama_idle_state(db: &Db) -> OllamaIdleState {
    state_from(db, launcher_env_override())
}

/// Validate and store the idle time (`None` = back to the default).
/// Refuses what the hub could not honour rather than storing a value that
/// silently falls back.
pub fn set_ollama_idle_with_db(db: &Db, value: Option<&str>) -> Result<OllamaIdleState, String> {
    match value.map(str::trim).filter(|v| !v.is_empty()) {
        None => {
            db.app_state_delete(lt::IDLE_APP_STATE_KEY)?;
        }
        Some(v) => {
            let (normalised, _) = lt::validate_idle(v)?;
            db.app_state_set(lt::IDLE_APP_STATE_KEY, &normalised)?;
        }
    }
    Ok(ollama_idle_state(db))
}

#[command]
pub async fn get_ollama_idle_unload(db: State<'_, Db>) -> Result<OllamaIdleState, String> {
    Ok(ollama_idle_state(&db))
}

/// `value` empty or `null` = use the default again.
#[command]
pub async fn set_ollama_idle_unload(
    value: Option<String>,
    db: State<'_, Db>,
) -> Result<OllamaIdleState, String> {
    set_ollama_idle_with_db(&db, value.as_deref())
}

/// The hub's list of loaded Ollama models (`GET /api/v1/ollama/models`).
#[command]
pub async fn ollama_models_status() -> Result<Value, String> {
    crate::commands::hub_proxy::hub_get("/ollama/models").await
}

/// Unload one model now (`POST /api/v1/ollama/models/unload`).
#[command]
pub async fn ollama_unload_model(model: String) -> Result<Value, String> {
    let model = model.trim().to_string();
    if model.is_empty() {
        return Err("name the model to unload".into());
    }
    crate::commands::hub_proxy::hub_post(
        "/ollama/models/unload",
        &serde_json::json!({ "model": model }),
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unset_reports_the_default() {
        let db = Db::open_in_memory().unwrap();
        let st = state_from(&db, None);
        assert_eq!(st.stored, None);
        assert_eq!(st.effective, "10m");
        assert_eq!(st.default_idle, "10m");
        assert_eq!(st.max_idle, "30m");
        assert_eq!(st.caller_keep_alive, "45m");
    }

    /// Round trip: what the command stores is what the hub resolves.
    #[test]
    fn set_round_trips_through_the_hub_resolver() {
        let db = Db::open_in_memory().unwrap();
        let st = set_ollama_idle_with_db(&db, Some(" 120 ")).unwrap();
        assert_eq!(st.stored.as_deref(), Some("2m"), "stored normalised");
        assert_eq!(st.effective_secs, 120);
        assert_eq!(lt::idle_from_db(&db).secs, 120, "the hub reads the same value");
        let st = set_ollama_idle_with_db(&db, Some("0")).unwrap();
        assert_eq!(st.effective, "0");
    }

    #[test]
    fn invalid_or_too_long_values_are_refused_and_leave_the_row_alone() {
        let db = Db::open_in_memory().unwrap();
        set_ollama_idle_with_db(&db, Some("5m")).unwrap();
        for bad in ["-1", "forever", "31m", "1.5m"] {
            assert!(set_ollama_idle_with_db(&db, Some(bad)).is_err(), "{bad}");
        }
        assert_eq!(lt::idle_from_db(&db).secs, 300);
    }

    #[test]
    fn clearing_returns_to_the_default() {
        let db = Db::open_in_memory().unwrap();
        set_ollama_idle_with_db(&db, Some("30m")).unwrap();
        let st = set_ollama_idle_with_db(&db, None).unwrap();
        assert_eq!((st.stored, st.effective_secs), (None, 600));
        let st = set_ollama_idle_with_db(&db, Some("  ")).unwrap();
        assert_eq!(st.stored, None);
    }

    #[test]
    fn a_hand_edited_row_is_flagged_not_honoured() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(lt::IDLE_APP_STATE_KEY, "never").unwrap();
        let st = state_from(&db, None);
        assert!(st.stored_invalid);
        assert_eq!(st.effective_secs, 600);
    }

    #[test]
    fn the_env_override_is_reported() {
        let db = Db::open_in_memory().unwrap();
        assert_eq!(state_from(&db, Some("-1".into())).env_override.as_deref(), Some("-1"));
    }
}
