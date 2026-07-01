// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Per-project `worktree_repo_mode` — GUI-only tri-state setting backing the
//! subagent-git modal (v0.2.71 Track T-WT).
//!
//! ## What this is
//! When a project's workspace ROOT is not inside any git repo, the
//! closed-source Claude Code harness's `git worktree add` (driven by the 9
//! agents that declare `isolation: worktree`) fails the subagent spawn. VCO
//! cannot intercept that spawn, so its only levers are: detect/use an
//! enclosing repo, offer to `git init` a local-only repo, or strip the
//! `isolation: worktree` frontmatter so the harness never attempts a
//! worktree. The subagent-git modal surfaces those choices; this setting
//! records the user's decision per-project.
//!
//! Tri-state (a string, NOT a bool — three states):
//!   * `"use_existing"` — an enclosing repo was detected (root or a parent
//!     has `.git`); the harness walks up to it, isolation already works.
//!     Recorded automatically when detection succeeds; the user never has
//!     to click it.
//!   * `"local_init"` — user accepted a local-only `git init` at the
//!     workspace root.
//!   * `"no_repo"` — user opted out; subagents run in the shared cwd (no
//!     isolation). The enforcement (frontmatter strip) is owned by the
//!     project_init / install-bundle flow keyed off this setting — NOT by
//!     this command, which only persists the choice.
//!
//! ## GUI-only, NOT hub-resolved
//! Deliberately stored in `module_settings` (module_id =
//! "orchestrator-core") and read back only via the launcher GUI. It is NOT
//! added to `config_api.rs`'s `ProjectConfigResponse` because the harness
//! spawn pathway (the only consumer that would need a hub-resolved value)
//! cannot be intercepted by VCO — there is nothing on the MCP/hook side that
//! resolves this at spawn time. Keeping it GUI-only avoids a hub field that
//! no subprocess reads.
//!
//! Plumbing mirrors the `shared_kg_read_disabled` / RL-flag pattern
//! (`rl_settings.rs:set_rl_use_global`) but with a string value instead of a
//! bool, since this is tri-state.

use serde_json::Value;
use tauri::{command, State};

use crate::db::Db;

/// Module id under which the orchestrator's own per-project settings live.
const MODULE_ID: &str = "orchestrator-core";

/// Setting key for the tri-state worktree-repo mode.
const SETTING_KEY: &str = "worktree_repo_mode";

/// The three valid modes. Any other value is rejected by the setter so a
/// GUI bug can't persist garbage the modal then can't interpret.
const VALID_MODES: [&str; 3] = ["use_existing", "local_init", "no_repo"];

/// Persist the per-project worktree-repo mode.
///
/// Rejects an empty `project_id` (caller bug, not a soft-fail) and any
/// `mode` outside the tri-state set.
#[command]
pub async fn set_worktree_repo_mode(
    project_id: String,
    mode: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_worktree_repo_mode: project_id required".into());
    }
    if !VALID_MODES.contains(&mode.as_str()) {
        return Err(format!(
            "set_worktree_repo_mode: invalid mode '{}' (expected one of {:?})",
            mode, VALID_MODES
        ));
    }
    db.set_setting(&project_id, MODULE_ID, SETTING_KEY, &Value::String(mode))
}

/// Read back the per-project worktree-repo mode.
///
/// Returns `None` (serialised as JSON `null`) when no choice has been
/// recorded yet — the modal uses that to know it should prompt. A non-string
/// or unknown stored value is coerced to `None` (defensive: never hand the
/// GUI a value outside the tri-state contract).
#[command]
pub async fn get_worktree_repo_mode(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    if project_id.is_empty() {
        return Err("get_worktree_repo_mode: project_id required".into());
    }
    let stored = db
        .get_setting(&project_id, MODULE_ID, SETTING_KEY)?
        .and_then(|v| v.as_str().map(|s| s.to_string()));
    Ok(match stored {
        Some(s) if VALID_MODES.contains(&s.as_str()) => Some(s),
        _ => None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_modes_are_exactly_the_tri_state() {
        // Pins the tri-state contract so a future edit that adds/removes a
        // mode must consciously update both the const and this test (and the
        // modal's option set).
        assert_eq!(VALID_MODES.len(), 3);
        assert!(VALID_MODES.contains(&"use_existing"));
        assert!(VALID_MODES.contains(&"local_init"));
        assert!(VALID_MODES.contains(&"no_repo"));
    }

    #[test]
    fn module_id_is_orchestrator_core() {
        // The setting must land under orchestrator-core (same blob the
        // shared-KG / per-project orchestrator flags use), never under a
        // paid-module id.
        assert_eq!(MODULE_ID, "orchestrator-core");
        assert_eq!(SETTING_KEY, "worktree_repo_mode");
    }
}
