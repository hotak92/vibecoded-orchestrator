// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The model gateway's GLOBAL first-party auth mode — the homepage
//! "Anthropic: OAuth | API key" switch (v0.2.104, owner order 2026-10-18).
//!
//! ONE home for the setting: launcher.db `app_state`, key
//! [`ANTHROPIC_AUTH_APP_STATE_KEY`]. The gateway reads that row itself
//! (`model_router.config.anthropic_auth_mode`, re-read within seconds), so a
//! change applies without a gateway restart. This module only validates and
//! persists the user's choice; whether the API key RESOLVES is the gateway's
//! answer (`/health` → `anthropic_auth.api_key`), read by the frontend from
//! the status probe it already makes — never a second resolver here.
//!
//! A mode VALUE, not a boolean, so a later mode (another account, another
//! key) is one more entry in [`ANTHROPIC_AUTH_MODES`], not a redesign.

use tauri::{command, State};

/// MUST MATCH `ANTHROPIC_AUTH_APP_STATE_KEY` in
/// `claude_mcp_servers/model_router/config.py` (parity-tested from Python:
/// `tests/test_v02104_gateway_api_key_route.py`).
pub const ANTHROPIC_AUTH_APP_STATE_KEY: &str = "model_gateway.anthropic_auth";

/// The default: the Claude login, today's behaviour.
pub const ANTHROPIC_AUTH_OAUTH: &str = "oauth";

/// MUST MATCH `ANTHROPIC_AUTH_MODES` in `model_router/config.py`, same order.
pub const ANTHROPIC_AUTH_MODES: [&str; 2] = ["oauth", "api_key"];

/// The persisted mode. An absent, unreadable or unknown row reads
/// [`ANTHROPIC_AUTH_OAUTH`] — the same rule the gateway applies, so the
/// switch never shows a mode the gateway is not using.
pub fn read_mode(db: &crate::db::Db) -> String {
    match db.app_state_get(ANTHROPIC_AUTH_APP_STATE_KEY) {
        Ok(Some(raw)) => {
            let value = raw.trim().to_ascii_lowercase();
            if ANTHROPIC_AUTH_MODES.contains(&value.as_str()) {
                value
            } else {
                ANTHROPIC_AUTH_OAUTH.to_string()
            }
        }
        _ => ANTHROPIC_AUTH_OAUTH.to_string(),
    }
}

/// Validate and persist. An unknown mode is refused, never stored: a value
/// the gateway would read as `oauth` must not be shown as something else.
pub fn write_mode(db: &crate::db::Db, mode: &str) -> Result<String, String> {
    let value = mode.trim().to_ascii_lowercase();
    if !ANTHROPIC_AUTH_MODES.contains(&value.as_str()) {
        return Err(format!(
            "unknown Anthropic auth mode {:?}; expected one of {:?}",
            mode, ANTHROPIC_AUTH_MODES
        ));
    }
    db.app_state_set(ANTHROPIC_AUTH_APP_STATE_KEY, &value)?;
    Ok(value)
}

#[command]
pub async fn model_gateway_anthropic_auth_get(
    db: State<'_, crate::db::Db>,
) -> Result<String, String> {
    Ok(read_mode(&db))
}

#[command]
pub async fn model_gateway_anthropic_auth_set(
    mode: String,
    db: State<'_, crate::db::Db>,
) -> Result<String, String> {
    write_mode(&db, &mode)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_mode_defaults_to_oauth_and_round_trips() {
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        assert_eq!(read_mode(&db), "oauth", "no row reads oauth");
        assert_eq!(write_mode(&db, "api_key").unwrap(), "api_key");
        assert_eq!(read_mode(&db), "api_key");
        assert_eq!(
            db.app_state_get(ANTHROPIC_AUTH_APP_STATE_KEY).unwrap().as_deref(),
            Some("api_key"),
            "persisted under the key the gateway reads",
        );
        assert_eq!(write_mode(&db, " OAuth ").unwrap(), "oauth");
        assert_eq!(read_mode(&db), "oauth");
    }

    #[test]
    fn an_unknown_mode_is_refused_and_leaves_the_row_alone() {
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        write_mode(&db, "api_key").unwrap();
        assert!(write_mode(&db, "second-account").is_err());
        assert_eq!(read_mode(&db), "api_key");
    }

    #[test]
    fn a_damaged_row_reads_oauth() {
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        db.app_state_set(ANTHROPIC_AUTH_APP_STATE_KEY, "garbage").unwrap();
        assert_eq!(read_mode(&db), "oauth");
    }
}
