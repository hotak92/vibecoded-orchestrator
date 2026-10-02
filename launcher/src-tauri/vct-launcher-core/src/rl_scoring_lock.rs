// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! The RL scoring lock (v0.2.100, W5R-02) — Rust reader of the ONE home,
//! `vco_lib/rl_scoring_lock.toml` (Python reads the same file through
//! `vco_lib/rl_scoring_lock.py`; the launcher GUI reads what this module
//! serves, never a constant of its own).
//!
//! OWNER (2026-10-01): "wire it, but for now we are keeping the RL module off
//! because we still didn't train the neural network, so keep it unactive for
//! now". While the table says `locked = true`:
//!
//!   * [`crate::db::Db::rl_scoring_enabled_for_project`] (what the hub's
//!     `/config` serves as `rl_reranker_enabled_for_project`) is `false`
//!     regardless of per-project / host-wide rows;
//!   * the GUI renders every RL scoring control locked, with [`rl_scoring_lock`]'s
//!     reason, and the Tauri setters refuse a "turn on" write
//!     ([`refuse_enable_while_locked`]).
//!
//! It gates SCORING only. The module-enable cascade (which projects receive
//! the module's settings) and event collection (`rl_events`) never read it.
//! Stored rows are never rewritten: they apply again once unlocked.

use std::sync::LazyLock;

use serde::Deserialize;

use crate::db::settings::RL_RERANKER_MODULE_ID;

/// Embedded copy of the table, read at compile time. Path: 4 levels up from
/// this file (`src` → `vct-launcher-core` → `src-tauri` → `launcher` → repo
/// root), then into `vco_lib/` — the same shape as `mcp_scan_rules.rs`.
const RL_SCORING_LOCK_TOML: &str = include_str!("../../../../vco_lib/rl_scoring_lock.toml");

const SUPPORTED_FORMAT_VERSION: u32 = 1;

/// Reason used when the embedded table cannot be parsed. Unreachable in a
/// tested build (the `shipped_table_parses` test pins the parse); if it ever
/// fires, scoring is treated as LOCKED — the conservative answer when
/// "scoring is allowed" cannot be positively confirmed.
const UNREADABLE_REASON: &str =
    "RL scoring lock table unreadable (broken build); RL scoring is treated as locked.";

#[derive(Debug, Deserialize)]
struct Wire {
    format_version: u32,
    locked: bool,
    reason: String,
}

/// Parse a lock table. `Ok(Some(reason))` = locked, `Ok(None)` = unlocked.
pub fn parse_lock(text: &str) -> Result<Option<String>, String> {
    let w: Wire = toml::from_str(text).map_err(|e| format!("rl_scoring_lock.toml: {e}"))?;
    if w.format_version != SUPPORTED_FORMAT_VERSION {
        return Err(format!(
            "rl_scoring_lock.toml: unsupported format_version {} (this reader reads {})",
            w.format_version, SUPPORTED_FORMAT_VERSION
        ));
    }
    if !w.locked {
        return Ok(None);
    }
    if w.reason.trim().is_empty() {
        return Err("rl_scoring_lock.toml: `reason` must be non-empty while locked".to_string());
    }
    Ok(Some(w.reason))
}

static SHIPPED: LazyLock<Option<String>> = LazyLock::new(|| match parse_lock(RL_SCORING_LOCK_TOML) {
    Ok(v) => v,
    Err(e) => {
        tracing::error!("[rl-scoring-lock] {e}; treating RL scoring as locked");
        Some(UNREADABLE_REASON.to_string())
    }
});

/// The reason RL scoring is locked off, or `None` when it is not locked.
pub fn rl_scoring_lock() -> Option<&'static str> {
    SHIPPED.as_deref()
}

/// The lock that applies to `module_id`'s RL scoring effect: the RL scoring
/// lock for the RL reranker, `None` for every other module.
pub fn module_effect_lock(module_id: &str) -> Option<&'static str> {
    if module_id == RL_RERANKER_MODULE_ID {
        rl_scoring_lock()
    } else {
        None
    }
}

/// Refuse a write that would turn `module_id` ON while its effect is locked.
/// Turning it off, or clearing a row, is always allowed (neither can turn
/// scoring on, and both are the user's data to manage).
pub fn refuse_enable_while_locked(module_id: &str, enabling: bool) -> Result<(), String> {
    refuse_enable_with_lock(module_effect_lock(module_id), enabling)
}

/// [`refuse_enable_while_locked`] with an explicit lock (testable both ways).
pub fn refuse_enable_with_lock(lock: Option<&str>, enabling: bool) -> Result<(), String> {
    match lock {
        Some(reason) if enabling => Err(format!("RL scoring is locked: {reason}")),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shipped_table_parses_and_is_locked_with_the_owner_reason() {
        let parsed = parse_lock(RL_SCORING_LOCK_TOML).expect("shipped table parses");
        let reason = parsed.expect("v0.2.100 ships LOCKED (owner 2026-10-01)");
        assert!(reason.contains("until the model is trained"), "{reason}");
        assert!(reason.contains("Data collection continues"), "{reason}");
        assert_eq!(rl_scoring_lock(), Some(reason.as_str()));
    }

    #[test]
    fn the_lock_applies_to_the_rl_reranker_only() {
        assert!(module_effect_lock(RL_RERANKER_MODULE_ID).is_some());
        assert_eq!(module_effect_lock("vct-coordination"), None);
    }

    #[test]
    fn parse_accepts_unlocked_and_rejects_broken_tables() {
        assert_eq!(parse_lock("format_version = 1\nlocked = false\nreason = \"r\"\n"), Ok(None));
        assert!(parse_lock("format_version = 2\nlocked = true\nreason = \"r\"\n").is_err());
        assert!(parse_lock("format_version = 1\nlocked = true\nreason = \"\"\n").is_err());
        assert!(parse_lock("format_version = 1\nlocked = \"yes\"\nreason = \"r\"\n").is_err());
        assert!(parse_lock("not toml [").is_err());
    }

    #[test]
    fn refusal_blocks_only_turning_on_while_locked() {
        assert!(refuse_enable_with_lock(Some("r"), true).is_err());
        assert!(refuse_enable_with_lock(Some("r"), false).is_ok());
        assert!(refuse_enable_with_lock(None, true).is_ok());
        assert!(refuse_enable_while_locked("vct-coordination", true).is_ok());
        assert!(refuse_enable_while_locked(RL_RERANKER_MODULE_ID, true).is_err());
    }
}
