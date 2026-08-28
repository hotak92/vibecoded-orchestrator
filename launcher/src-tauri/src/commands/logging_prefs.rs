// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! v0.2.91 WP-L (plan decision #21) — the GUI surface for the machine-global
//! diagnostic log level.
//!
//! ## Why this is a dedicated command and not a generic `app_state_set`
//!
//! `logging_level` was DELETED from the Preferences page this same release
//! for being a toggle nothing read. Re-adding a level picker is only
//! defensible because the consumers now exist, and this file is where that
//! claim is made concrete — it does the three things a generic key write
//! cannot:
//!
//!   1. **Validates.** Only `error|warn|info|debug` are accepted. An
//!      unknown value is an explicit error rather than a row that silently
//!      resolves back to INFO — "a key the command refuses is a toggle that
//!      errors on click, which is the same lie with extra steps", so the
//!      command must not accept one it cannot honour either.
//!   2. **Takes effect immediately in THIS process.**
//!      `crate::logging::apply_stored_level` re-resolves and reloads the
//!      installed `tracing` filter, so the running launcher changes verbosity
//!      without a restart. (`VCO_LOG_LEVEL` still wins — the resolver is
//!      offered the env value again rather than assumed absent.)
//!   3. **Re-projects every project's env.** The level is projected as
//!      `VCO_LOG_LEVEL` into each project's `.claude/{settings.json,env}` by
//!      `vco_lib/config_projection.py`, which is how project-side hooks,
//!      MCPs and helper scripts see it. Writing app_state and staying silent
//!      would leave a control whose displayed state runs ahead of the
//!      effective one.
//!
//! What it deliberately does NOT do: reach into the detached `vct-hub`. The
//! hub resolves its own level at startup
//! (`VCO_LOG_LEVEL` > `app_state['logging.level']` > INFO), so it adopts a
//! change on its next start. The GUI copy says so rather than implying a
//! reach the launcher does not have.
//!
//! ## Scope (decision #21)
//!
//! DIAGNOSTICS ONLY. Telemetry `rl_events` and audit trails are data and
//! records, not diagnostics, and are never level-gated — no code here or
//! downstream may make them so.

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;
use vct_launcher_core::logging as core_logging;

/// The levels the GUI offers, in order, most severe first. Mirrors
/// `core_logging::parse_level`'s accepted set — that function is private, so
/// `logging_level_is_valid_or_err` below is the single validation point and
/// `levels_match_the_resolver` (tests) pins the two together.
pub const LOG_LEVELS: [&str; 4] = ["error", "warn", "info", "debug"];

/// Current state of the level preference.
#[derive(Debug, Clone, Serialize)]
pub struct LoggingLevelState {
    /// The stored `app_state` value, or `None` when no row exists (the GUI
    /// then shows the default without claiming the user picked it).
    pub stored: Option<String>,
    /// What this process is actually running at right now, lowercased.
    /// Differs from `stored` when `VCO_LOG_LEVEL` is set in the environment.
    pub effective: String,
    /// `true` when `VCO_LOG_LEVEL` is set, i.e. the stored preference is
    /// being overridden for this run. The GUI says so instead of rendering a
    /// picker whose value is not in force.
    pub env_override: bool,
    /// The level applied when nothing is stored.
    pub default_level: String,
}

fn logging_level_is_valid_or_err(level: &str) -> Result<String, String> {
    let normalized = level.trim().to_ascii_lowercase();
    if LOG_LEVELS.contains(&normalized.as_str()) {
        Ok(normalized)
    } else {
        Err(format!(
            "unknown log level '{}' (expected one of: {})",
            level,
            LOG_LEVELS.join(", ")
        ))
    }
}

/// Read the current level preference plus what is actually in force.
#[command]
pub async fn get_logging_level(db: State<'_, Db>) -> Result<LoggingLevelState, String> {
    Ok(logging_level_state(&db))
}

/// Free-function core of [`get_logging_level`], testable without Tauri.
pub fn logging_level_state(db: &Db) -> LoggingLevelState {
    // Soft-fail: an unreadable app_state reads as "nothing stored", which
    // the resolver turns into the env value or the default.
    let stored = db
        .app_state_get(core_logging::LOG_LEVEL_APP_STATE_KEY)
        .ok()
        .flatten();
    let env_value = core_logging::env_log_level();
    let effective =
        core_logging::resolve_log_level(env_value.as_deref(), stored.as_deref());
    LoggingLevelState {
        stored,
        effective: effective.as_str().to_ascii_lowercase(),
        env_override: env_value.is_some(),
        default_level: core_logging::DEFAULT_LOG_LEVEL
            .as_str()
            .to_ascii_lowercase(),
    }
}

/// Free-function core of [`set_logging_level`] — validate, persist, apply to
/// this process, then re-project every project's env. Testable without a
/// Tauri runtime.
///
/// Soft-fail after the write: the app_state row is the authoritative
/// operation and has already committed when the re-projection runs, so a
/// projection hiccup is a warning, never a rolled-back preference.
pub fn set_logging_level_with_db(db: &Db, level: &str) -> Result<LoggingLevelState, String> {
    let level = logging_level_is_valid_or_err(level)?;
    db.app_state_set(core_logging::LOG_LEVEL_APP_STATE_KEY, &level)?;

    // (2) This process, now — no restart.
    crate::logging::apply_stored_level(db);

    // (3) Every project's `.claude/{settings.json,env}`, so project-side
    // hooks / MCPs / scripts read the level the user just picked.
    let report = crate::commands::projects_v2::refresh_all_projects_env_with_db(db);
    for (name, err) in &report.failed {
        tracing::warn!(
            "[vct] warning: logging.level env re-projection failed for {}: {} \
             (preference already saved)",
            name,
            err
        );
    }
    Ok(logging_level_state(db))
}

/// Persist the machine-global diagnostic log level.
#[command]
pub async fn set_logging_level(
    level: String,
    db: State<'_, Db>,
) -> Result<LoggingLevelState, String> {
    set_logging_level_with_db(&db, &level)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn db_with_project() -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_project(
            "p-log",
            "LogProj",
            "/nonexistent/log-proj",
            crate::db::models::ProjectHost::Base,
            "logproj",
        )
        .expect("insert project");
        db
    }

    /// Every level the GUI offers must be one the resolver accepts. A picker
    /// entry the resolver silently drops to INFO is a control that lies about
    /// what it did.
    #[test]
    fn levels_match_the_resolver() {
        for level in LOG_LEVELS {
            let resolved = core_logging::resolve_log_level(Some(level), None);
            assert_eq!(
                resolved.as_str().to_ascii_lowercase(),
                level,
                "the GUI offers '{level}' but the resolver does not honour it",
            );
        }
    }

    /// The write command refuses what it cannot honour, rather than storing a
    /// value that quietly resolves back to INFO.
    #[test]
    fn unknown_levels_are_refused() {
        assert!(logging_level_is_valid_or_err("trace").is_err());
        assert!(logging_level_is_valid_or_err("").is_err());
        assert!(logging_level_is_valid_or_err("loud").is_err());
        // Case + surrounding whitespace are normalised, not rejected.
        assert_eq!(logging_level_is_valid_or_err(" WARN ").unwrap(), "warn");
    }

    /// Nothing stored ⇒ the GUI is told the default applies, WITHOUT a
    /// `stored` value it could mistake for a user choice.
    #[test]
    fn state_distinguishes_stored_from_default() {
        let db = db_with_project();
        let st = logging_level_state(&db);
        assert_eq!(st.stored, None);
        assert_eq!(st.default_level, "info");
    }

    /// Round-trip, plus proof the machine-global re-projection actually ran:
    /// a project whose folder does not exist can only land in `skipped` if
    /// the refresh-all path iterated the registered projects.
    #[test]
    fn set_persists_and_reprojects_every_project() {
        let db = db_with_project();
        let st = set_logging_level_with_db(&db, "debug").expect("write must succeed");
        assert_eq!(st.stored.as_deref(), Some("debug"));
        assert_eq!(
            db.app_state_get(core_logging::LOG_LEVEL_APP_STATE_KEY)
                .unwrap()
                .as_deref(),
            Some("debug"),
        );
        let report = crate::commands::projects_v2::refresh_all_projects_env_with_db(&db);
        assert!(
            report.skipped.contains(&"LogProj".to_string()),
            "the setter's re-projection must iterate registered projects",
        );
    }

    /// A rejected level must not have touched the stored row.
    #[test]
    fn a_refused_level_leaves_the_stored_value_alone() {
        let db = db_with_project();
        set_logging_level_with_db(&db, "warn").unwrap();
        assert!(set_logging_level_with_db(&db, "trace").is_err());
        assert_eq!(
            db.app_state_get(core_logging::LOG_LEVEL_APP_STATE_KEY)
                .unwrap()
                .as_deref(),
            Some("warn"),
            "a refused write must not clobber the previous preference",
        );
    }

    /// The key this command writes MUST be the one the consumers read. The
    /// legacy `logging_level` name is the dead key removed this release;
    /// writing it again would resurrect the no-op.
    #[test]
    fn writes_the_key_the_consumers_read() {
        assert_eq!(core_logging::LOG_LEVEL_APP_STATE_KEY, "logging.level");
        assert_ne!(core_logging::LOG_LEVEL_APP_STATE_KEY, "logging_level");
    }
}
