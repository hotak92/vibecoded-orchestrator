// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Machine-global on/off switch for Claude Code's **Artifact tool**.
//!
//! ## What it edits, and why that file
//!
//! Claude Code ships the `Artifact` tool's description in the system prompt of
//! every request. Users who never create artifacts pay those tokens on every
//! turn. Claude Code's own USER-scope settings file — `~/.claude/settings.json`
//! — is where that is turned off, so this is the file the toggle edits. It is
//! NOT `~/.claude.json` (MCP registrations; off-limits per the repo's
//! CLAUDE.md) and NOT launcher.db: the harness reads the JSON file, so the
//! JSON file is the source of truth and the GUI reads its state back from
//! there rather than from a mirrored launcher row that could disagree with it.
//!
//! ## The two keys, and why BOTH
//!
//! ```jsonc
//! {
//!   "enableArtifact": false,
//!   "permissions": { "deny": ["Artifact"] }
//! }
//! ```
//!
//! * `permissions.deny` with the **bare** tool name `"Artifact"` is the entry
//!   that actually saves tokens: a bare name removes the tool from Claude's
//!   context entirely, so the description is never sent. The SCOPED form
//!   `Artifact(*)` does NOT — it leaves the description in context and only
//!   blocks the call. This module therefore never writes the scoped form.
//! * `enableArtifact: false` is the purpose-built off-switch. On CLI versions
//!   before 2.1.242 a higher-precedence settings file can override it, which
//!   is exactly why the deny entry is kept alongside it: deny rules apply
//!   additively from every loaded settings file.
//!
//! Either key alone is enough to call artifacts DISABLED (that is how the
//! state is read back), but a disable WRITES both, and a partially-applied
//! file is reported as such rather than silently normalised on read.
//!
//! ## Enable direction (documented choice)
//!
//! Re-enabling REMOVES `enableArtifact` rather than setting it to `true`, and
//! removes the bare `"Artifact"` deny entry. Rationale: `true` would pin
//! today's Claude Code default into a user-owned file forever and fight a
//! future default change, whereas removal restores "no opinion recorded" —
//! which is what the user had before VCO touched anything. Structures this
//! module CREATED and then emptied (`permissions.deny`, `permissions`) are
//! pruned so a disable→enable round-trip leaves no residue.
//!
//! A user-authored SCOPED `Artifact(...)` deny entry is left alone in both
//! directions — VCO did not write it and does not own it — but its presence
//! is reported so the GUI can say that artifacts are still call-blocked by a
//! rule this toggle does not manage.
//!
//! ## User-owned file discipline
//!
//! This file holds the user's own configuration (`model`, `effortLevel`,
//! `env`, `features`, hand-written `_comment` keys, their whole
//! `permissions.allow` list…). Every write is a read-modify-write that
//! preserves every key it does not own, including unknown ones. Writes go
//! through `crate::json_file` (advisory lock, temp+rename, one-time backup).
//! A file that exists but does not parse is NEVER overwritten: the state
//! reads back as unknown, the GUI disables the control and shows the parse
//! error, and the bytes stay exactly as the user left them.
//!
//! A write is still a full re-serialisation, but since v0.2.92 `serde_json`'s
//! `preserve_order` feature is enabled for every launcher crate, so the keys
//! come back in the order the user had them. Nothing is dropped, nothing is
//! reordered, nothing is re-valued
//! (`a_write_preserves_key_order_and_never_drops_or_changes_a_value` pins all
//! three), and the same is true of `~/.claude.json` and every per-project
//! `.claude/settings.json`, which go through the same writer.
//!
//! What this does NOT do is repair a file an OLDER launcher already
//! alphabetised. VCO never recorded the original order, so it cannot restore
//! one; a build that alphabetised the file has destroyed the information.
//! Those files simply stay as they are — VCO does not rewrite a file merely to
//! reorder it, because a write it did not need to make is itself churn on a
//! user-owned file. The one-time `.vco-backup` sidecar taken before the first
//! VCO write holds the pre-VCO bytes and is the only recovery path; the GUI
//! says so.

use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;
use tauri::command;

use crate::json_file::{self, BackupPolicy};

/// Root key: Claude Code's purpose-built Artifact off-switch.
const ENABLE_ARTIFACT_KEY: &str = "enableArtifact";
/// The BARE deny entry — the one that removes the tool description from
/// context. Never write `Artifact(...)`; a scoped rule blocks the call but
/// keeps the tokens.
const DENY_BARE_ARTIFACT: &str = "Artifact";
/// Prefix identifying the scoped (token-wasting) deny form.
const DENY_SCOPED_PREFIX: &str = "Artifact(";

/// Extension of the one-time sidecar copy of the user's pre-VCO file.
const BACKUP_EXT: &str = "vco-backup";
const BACKUP_POLICY: BackupPolicy = BackupPolicy::Once { ext: BACKUP_EXT };

/// Wire shape consumed by `$lib/artifact-tool.ts`.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ArtifactToolState {
    /// Absolute path of the settings file this state was read from.
    pub settings_path: String,
    /// Whether that file exists on disk right now.
    pub file_exists: bool,
    /// `Some(true)` = artifacts enabled, `Some(false)` = disabled,
    /// `None` = **unknown** (file unreadable or not valid JSON). The GUI must
    /// render `None` as an unknown state with a disabled control, never as a
    /// guessed default.
    pub artifacts_enabled: Option<bool>,
    /// `enableArtifact: false` is present.
    pub enable_artifact_false: bool,
    /// `permissions.deny` contains the bare `"Artifact"`.
    pub deny_bare_artifact: bool,
    /// `permissions.deny` contains a scoped `Artifact(...)` rule. Not written
    /// or removed by VCO; reported because it blocks calls without saving
    /// tokens.
    pub deny_scoped_artifact: bool,
    /// Both managed keys are present — the state a disable writes. Computed
    /// here rather than re-derived in TypeScript so the "partially applied"
    /// rule has exactly one implementation.
    pub fully_disabled: bool,
    /// Parse/read failure text. Present ⟺ `artifacts_enabled` is `None`.
    pub error: Option<String>,
    /// Path of the one-time backup, when one exists on disk.
    pub backup_path: Option<String>,
}

/// `~/.claude/settings.json` — Claude Code's USER-scope settings file.
///
/// Falls back to a relative path only when the home directory cannot be
/// resolved at all, which the caller surfaces as a read error rather than
/// writing somewhere unexpected.
pub fn user_claude_settings_json() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".claude").join("settings.json"))
        .unwrap_or_else(|| PathBuf::from(".claude").join("settings.json"))
}

/// Detected positions of the two managed keys (plus the unmanaged scoped
/// form) in an already-parsed settings document.
struct ArtifactFlags {
    enable_artifact_false: bool,
    deny_bare: bool,
    deny_scoped: bool,
}

fn detect_flags(root: &Value) -> ArtifactFlags {
    let enable_artifact_false = matches!(root.get(ENABLE_ARTIFACT_KEY), Some(Value::Bool(false)));

    let mut deny_bare = false;
    let mut deny_scoped = false;
    if let Some(entries) = root
        .get("permissions")
        .and_then(|p| p.get("deny"))
        .and_then(|d| d.as_array())
    {
        for entry in entries {
            // Non-string entries are somebody else's schema; ignore for
            // detection, preserve verbatim on write.
            let Some(s) = entry.as_str() else { continue };
            let s = s.trim();
            if s == DENY_BARE_ARTIFACT {
                deny_bare = true;
            } else if s.starts_with(DENY_SCOPED_PREFIX) {
                deny_scoped = true;
            }
        }
    }

    ArtifactFlags {
        enable_artifact_false,
        deny_bare,
        deny_scoped,
    }
}

fn backup_path_if_present(path: &Path) -> Option<String> {
    let bak = json_file::sibling_path(path, BACKUP_EXT);
    bak.exists().then(|| bak.display().to_string())
}

/// Parse the settings document, refusing anything that is not a JSON object.
///
/// A non-object root (array, string, number) is treated exactly like a parse
/// failure: this is a user-owned file, and "replace the root with `{}`" —
/// which the per-project writer in `projects_v2` can afford, because the
/// launcher authors that file — would destroy the user's content here.
fn load_object(path: &Path) -> Result<Value, String> {
    let root = json_file::read_json_or_empty(path)?;
    if !root.is_object() {
        return Err(format!(
            "{} does not contain a JSON object at its root; refusing to modify it",
            path.display()
        ));
    }
    Ok(root)
}

/// Read the current state. Never writes, never creates the file — page load
/// must not author anything into a user-owned file.
pub fn read_artifact_state(path: &Path) -> ArtifactToolState {
    let file_exists = path.exists();
    let backup_path = backup_path_if_present(path);

    let root = match load_object(path) {
        Ok(v) => v,
        Err(e) => {
            return ArtifactToolState {
                settings_path: path.display().to_string(),
                file_exists,
                artifacts_enabled: None,
                enable_artifact_false: false,
                deny_bare_artifact: false,
                deny_scoped_artifact: false,
                fully_disabled: false,
                error: Some(e),
                backup_path,
            };
        }
    };

    let flags = detect_flags(&root);
    ArtifactToolState {
        settings_path: path.display().to_string(),
        file_exists,
        // Either managed key disables. Absent file / absent keys = Claude
        // Code's own default, which is ENABLED — the honest reading, not an
        // assumption of what VCO would like it to be.
        artifacts_enabled: Some(!(flags.enable_artifact_false || flags.deny_bare)),
        enable_artifact_false: flags.enable_artifact_false,
        deny_bare_artifact: flags.deny_bare,
        deny_scoped_artifact: flags.deny_scoped,
        fully_disabled: flags.enable_artifact_false && flags.deny_bare,
        error: None,
        backup_path,
    }
}

/// Apply the two managed keys for `enabled` to an already-parsed object.
///
/// Returns an error (leaving `root` untouched) when a key we must descend
/// through has an incompatible type — `permissions` that is not an object, or
/// `permissions.deny` that is not an array. Coercing those would silently
/// discard user content.
fn apply_enabled(root: &mut Value, enabled: bool) -> Result<(), String> {
    // Validate the shapes we are about to descend through BEFORE mutating
    // anything, so a rejection cannot leave a half-applied document.
    if let Some(perms) = root.get("permissions") {
        if !perms.is_object() {
            return Err("\"permissions\" is not a JSON object; refusing to modify it".to_string());
        }
        if let Some(deny) = perms.get("deny") {
            if !deny.is_array() {
                return Err(
                    "\"permissions.deny\" is not a JSON array; refusing to modify it".to_string(),
                );
            }
        }
    }

    let obj = root
        .as_object_mut()
        .ok_or_else(|| "settings root is not a JSON object".to_string())?;

    if !enabled {
        obj.insert(ENABLE_ARTIFACT_KEY.to_string(), Value::Bool(false));

        let perms = obj
            .entry("permissions".to_string())
            .or_insert_with(|| Value::Object(serde_json::Map::new()))
            .as_object_mut()
            .ok_or_else(|| "\"permissions\" is not a JSON object".to_string())?;
        let deny = perms
            .entry("deny".to_string())
            .or_insert_with(|| Value::Array(Vec::new()))
            .as_array_mut()
            .ok_or_else(|| "\"permissions.deny\" is not a JSON array".to_string())?;

        let already = deny
            .iter()
            .any(|v| v.as_str().map(|s| s.trim()) == Some(DENY_BARE_ARTIFACT));
        if !already {
            deny.push(Value::String(DENY_BARE_ARTIFACT.to_string()));
        }
        return Ok(());
    }

    // Enable: remove the off-switch rather than pinning `true` (see module
    // docs), and drop the bare deny entry.
    //
    // `shift_remove`, never `remove`: under `preserve_order` (enabled
    // workspace-wide since v0.2.92) `Map::remove` is `swap_remove`, which
    // fills the vacated slot with the map's LAST key. On a user-owned file
    // that would move an unrelated key across the document on every enable —
    // the exact reordering this feature exists to stop. `shift_remove` closes
    // the gap and leaves every other key where the user put it.
    obj.shift_remove(ENABLE_ARTIFACT_KEY);

    let Some(perms) = obj.get_mut("permissions").and_then(|p| p.as_object_mut()) else {
        return Ok(());
    };
    let mut removed_any = false;
    if let Some(deny) = perms.get_mut("deny").and_then(|d| d.as_array_mut()) {
        let before = deny.len();
        deny.retain(|v| v.as_str().map(|s| s.trim()) != Some(DENY_BARE_ARTIFACT));
        removed_any = deny.len() != before;
        if removed_any && deny.is_empty() {
            // Only prune a container we just emptied — never one the user
            // left empty themselves. `shift_remove` for the same reason as
            // above: `remove` would swap the last key into this slot.
            perms.shift_remove("deny");
        }
    }
    if removed_any && perms.is_empty() {
        obj.shift_remove("permissions");
    }
    Ok(())
}

/// Read-modify-write `path` so artifacts end up `enabled`, then return the
/// re-read state.
///
/// No-op safe: when the document already says what it should, nothing is
/// written (no mtime churn, no backup created).
pub fn set_artifact_enabled_at(path: &Path, enabled: bool) -> Result<ArtifactToolState, String> {
    // Enabling when no settings file exists is a no-op by definition (the
    // absent file already means "Claude Code's default"). Short-circuit
    // before the lock, so this path does not create `~/.claude/` — or a lock
    // file inside it — just to conclude there was nothing to do.
    if enabled && !path.exists() {
        return Ok(read_artifact_state(path));
    }

    let _lock = json_file::acquire_lock(path, 5000)?;

    let original = load_object(path)?;
    let mut updated = original.clone();
    apply_enabled(&mut updated, enabled)?;

    if updated != original {
        json_file::atomic_write_json(path, &updated, BACKUP_POLICY)?;
    }
    Ok(read_artifact_state(path))
}

/// Read the machine-global Artifact-tool state from `~/.claude/settings.json`.
#[command]
pub async fn get_artifact_tool_state() -> Result<ArtifactToolState, String> {
    Ok(read_artifact_state(&user_claude_settings_json()))
}

/// Turn the Artifact tool on or off machine-globally. Returns the state as
/// re-read from disk, so the GUI never renders an intent it did not achieve.
#[command]
pub async fn set_artifact_tool_enabled(enabled: bool) -> Result<ArtifactToolState, String> {
    let path = user_claude_settings_json();
    let state = set_artifact_enabled_at(&path, enabled)?;
    tracing::info!(
        "[vct] artifact tool set to enabled={} in {} (enableArtifact_false={}, deny_bare={})",
        enabled,
        state.settings_path,
        state.enable_artifact_false,
        state.deny_bare_artifact,
    );
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_settings(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "vct-artifact-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("settings.json")
    }

    fn cleanup(path: &Path) {
        if let Some(parent) = path.parent() {
            std::fs::remove_dir_all(parent).ok();
        }
    }

    fn read_back(path: &Path) -> Value {
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    /// A realistic user file: schema pin, comment-style keys no schema knows
    /// about, model/effort prefs, env, features, and their own allow list.
    const REAL_USER_FILE: &str = r#"{
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "_comment": "hand-written note that no schema knows about",
        "agentPushNotifEnabled": false,
        "effortLevel": "xhigh",
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "100000"},
        "features": {"autoCommit": false},
        "_hooks_removed_2026-05-16": "another hand-written key",
        "permissions": {
            "allow": ["Read(*)", "Write(*)", "Bash(ls:*)"],
            "deny": ["Bash(rm -rf:*)", "Bash(sudo:*)"]
        },
        "switchModelsOnFlag": false,
        "model": "opus[1m]"
    }"#;

    // ── reading ─────────────────────────────────────────────────────────

    #[test]
    fn missing_file_reads_as_enabled_which_is_claude_codes_own_default() {
        let path = tmp_settings("missing");
        let state = read_artifact_state(&path);
        assert!(!state.file_exists);
        assert_eq!(state.artifacts_enabled, Some(true));
        assert!(state.error.is_none());
        assert!(!state.enable_artifact_false && !state.deny_bare_artifact);
        // And reading must not have created anything.
        assert!(!path.exists(), "read must never author the file");
        cleanup(&path);
    }

    #[test]
    fn file_without_either_key_reads_as_enabled() {
        let path = tmp_settings("neither");
        std::fs::write(&path, REAL_USER_FILE).unwrap();
        let state = read_artifact_state(&path);
        assert_eq!(state.artifacts_enabled, Some(true));
        assert!(!state.fully_disabled);
        cleanup(&path);
    }

    /// R2's mixed state: EITHER key alone means disabled, and the state says
    /// which one is present so the GUI can offer to complete the pair.
    #[test]
    fn either_key_alone_reads_as_disabled_and_reports_the_partial_state() {
        let only_enable_flag = tmp_settings("only-flag");
        std::fs::write(&only_enable_flag, r#"{"enableArtifact": false}"#).unwrap();
        let s = read_artifact_state(&only_enable_flag);
        assert_eq!(s.artifacts_enabled, Some(false));
        assert!(s.enable_artifact_false && !s.deny_bare_artifact);
        assert!(!s.fully_disabled, "one key is not the full disable");
        cleanup(&only_enable_flag);

        let only_deny = tmp_settings("only-deny");
        std::fs::write(&only_deny, r#"{"permissions": {"deny": ["Artifact"]}}"#).unwrap();
        let s = read_artifact_state(&only_deny);
        assert_eq!(s.artifacts_enabled, Some(false));
        assert!(s.deny_bare_artifact && !s.enable_artifact_false);
        assert!(!s.fully_disabled);
        cleanup(&only_deny);
    }

    /// The state the mechanism produces when applied BY HAND (which is how it
    /// reached this machine before the GUI existed) must read back as "off,
    /// fully applied" — the GUI's initial position has to be the file's real
    /// state, not a default the panel prefers.
    #[test]
    fn a_hand_applied_disable_reads_back_as_fully_disabled() {
        let path = tmp_settings("hand-applied");
        std::fs::write(
            &path,
            r#"{
                "_comment": "hand-written",
                "effortLevel": "xhigh",
                "permissions": {
                    "allow": ["Read(*)", "Bash(ls:*)"],
                    "deny": ["Bash(rm -rf:*)", "Bash(sudo:*)", "Artifact"]
                },
                "model": "opus[1m]",
                "enableArtifact": false
            }"#,
        )
        .unwrap();
        let s = read_artifact_state(&path);
        assert_eq!(s.artifacts_enabled, Some(false));
        assert!(s.fully_disabled);
        assert!(!s.deny_scoped_artifact);
        assert!(s.error.is_none());
        cleanup(&path);
    }

    /// The scoped form does NOT save tokens, so it must not be read as the
    /// bare entry — otherwise the GUI would claim a saving that is not real.
    #[test]
    fn scoped_deny_entry_is_reported_separately_and_does_not_count_as_disabled() {
        let path = tmp_settings("scoped");
        std::fs::write(&path, r#"{"permissions": {"deny": ["Artifact(*)"]}}"#).unwrap();
        let s = read_artifact_state(&path);
        assert!(s.deny_scoped_artifact);
        assert!(!s.deny_bare_artifact);
        assert_eq!(
            s.artifacts_enabled,
            Some(true),
            "a scoped rule blocks the call but leaves the description in context"
        );
        cleanup(&path);
    }

    #[test]
    fn enable_artifact_true_or_non_bool_does_not_read_as_disabled() {
        for body in [
            r#"{"enableArtifact": true}"#,
            r#"{"enableArtifact": "false"}"#,
        ] {
            let path = tmp_settings("enable-variants");
            std::fs::write(&path, body).unwrap();
            let s = read_artifact_state(&path);
            assert_eq!(s.artifacts_enabled, Some(true), "body was {}", body);
            assert!(!s.enable_artifact_false);
            cleanup(&path);
        }
    }

    // ── corrupt / hostile shapes are never clobbered ────────────────────

    #[test]
    fn corrupted_file_is_left_untouched_and_reported_as_unknown() {
        let path = tmp_settings("corrupt");
        let raw = "{ \"model\": \"opus\", this is not valid json";
        std::fs::write(&path, raw).unwrap();

        let state = read_artifact_state(&path);
        assert_eq!(state.artifacts_enabled, None, "unknown, never guessed");
        assert!(state.error.is_some());
        assert!(state.file_exists);

        // A write attempt must refuse, not repair-by-overwriting.
        let err = set_artifact_enabled_at(&path, false).expect_err("must refuse to write");
        assert!(err.contains("parse"), "unexpected error: {}", err);
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            raw,
            "the user's bytes must survive byte-for-byte"
        );
        assert!(
            !json_file::sibling_path(&path, BACKUP_EXT).exists(),
            "a refused write must not leave a backup either"
        );
        cleanup(&path);
    }

    #[test]
    fn non_object_root_is_refused_rather_than_replaced() {
        let path = tmp_settings("array-root");
        std::fs::write(&path, r#"["not", "an", "object"]"#).unwrap();
        assert_eq!(read_artifact_state(&path).artifacts_enabled, None);
        set_artifact_enabled_at(&path, false).expect_err("must refuse");
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            r#"["not", "an", "object"]"#
        );
        cleanup(&path);
    }

    #[test]
    fn incompatible_permissions_shapes_are_refused_without_partial_writes() {
        let not_object = tmp_settings("perms-not-object");
        std::fs::write(&not_object, r#"{"permissions": "everything"}"#).unwrap();
        let err = set_artifact_enabled_at(&not_object, false).expect_err("must refuse");
        assert!(err.contains("permissions"), "unexpected error: {}", err);
        assert_eq!(
            std::fs::read_to_string(&not_object).unwrap(),
            r#"{"permissions": "everything"}"#,
            "a refused write leaves no partial application (no enableArtifact either)"
        );
        cleanup(&not_object);

        let deny_not_array = tmp_settings("deny-not-array");
        std::fs::write(&deny_not_array, r#"{"permissions": {"deny": "Artifact"}}"#).unwrap();
        set_artifact_enabled_at(&deny_not_array, false).expect_err("must refuse");
        assert_eq!(
            std::fs::read_to_string(&deny_not_array).unwrap(),
            r#"{"permissions": {"deny": "Artifact"}}"#
        );
        cleanup(&deny_not_array);
    }

    // ── writing ─────────────────────────────────────────────────────────

    #[test]
    fn disable_writes_both_keys_and_preserves_every_unrelated_key() {
        let path = tmp_settings("disable");
        std::fs::write(&path, REAL_USER_FILE).unwrap();

        let state = set_artifact_enabled_at(&path, false).expect("write must succeed");
        assert_eq!(state.artifacts_enabled, Some(false));
        assert!(state.fully_disabled, "a disable writes BOTH keys");

        let v = read_back(&path);
        assert_eq!(v[ENABLE_ARTIFACT_KEY], Value::Bool(false));
        let deny = v["permissions"]["deny"].as_array().unwrap();
        assert!(deny.contains(&Value::String("Artifact".into())));
        assert!(
            !deny.iter().any(|e| e.as_str() == Some("Artifact(*)")),
            "the scoped form must never be written — it does not save tokens"
        );

        // Everything the user owns survives, including keys no schema knows.
        assert_eq!(v["model"], "opus[1m]");
        assert_eq!(v["effortLevel"], "xhigh");
        assert_eq!(v["$schema"], "https://json.schemastore.org/claude-code-settings.json");
        assert_eq!(v["_comment"], "hand-written note that no schema knows about");
        assert_eq!(v["_hooks_removed_2026-05-16"], "another hand-written key");
        assert_eq!(v["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "100000");
        assert_eq!(v["features"]["autoCommit"], false);
        assert_eq!(v["agentPushNotifEnabled"], false);
        assert_eq!(v["switchModelsOnFlag"], false);
        assert_eq!(v["permissions"]["allow"].as_array().unwrap().len(), 3);
        // Their own deny rules are still there, ours is appended.
        assert!(deny.contains(&Value::String("Bash(rm -rf:*)".into())));
        assert!(deny.contains(&Value::String("Bash(sudo:*)".into())));

        cleanup(&path);
    }

    #[test]
    fn disable_creates_the_permissions_block_when_the_file_has_none() {
        let path = tmp_settings("no-perms");
        std::fs::write(&path, r#"{"model": "opus"}"#).unwrap();
        let state = set_artifact_enabled_at(&path, false).unwrap();
        assert!(state.fully_disabled);
        let v = read_back(&path);
        assert_eq!(v["permissions"]["deny"][0], "Artifact");
        assert_eq!(v["model"], "opus");
        cleanup(&path);
    }

    #[test]
    fn disable_on_a_missing_file_creates_a_minimal_one() {
        let path = tmp_settings("create");
        assert!(!path.exists());
        let state = set_artifact_enabled_at(&path, false).unwrap();
        assert_eq!(state.artifacts_enabled, Some(false));
        let v = read_back(&path);
        assert_eq!(v[ENABLE_ARTIFACT_KEY], Value::Bool(false));
        assert_eq!(v["permissions"]["deny"][0], "Artifact");
        cleanup(&path);
    }

    #[test]
    fn round_trip_leaves_no_stray_artifact_deny_entry_or_residue() {
        let path = tmp_settings("round-trip");
        let pristine = r#"{"model": "opus", "effortLevel": "high"}"#;
        std::fs::write(&path, pristine).unwrap();

        set_artifact_enabled_at(&path, false).unwrap();
        let state = set_artifact_enabled_at(&path, true).unwrap();

        assert_eq!(state.artifacts_enabled, Some(true));
        assert!(!state.deny_bare_artifact && !state.enable_artifact_false);

        let v = read_back(&path);
        assert!(
            v.get(ENABLE_ARTIFACT_KEY).is_none(),
            "enable removes the key rather than pinning true: {}",
            v
        );
        assert!(
            v.get("permissions").is_none(),
            "containers we created and then emptied are pruned: {}",
            v
        );
        assert_eq!(v["model"], "opus");
        assert_eq!(v["effortLevel"], "high");
        cleanup(&path);
    }

    #[test]
    fn enable_keeps_user_deny_rules_and_only_removes_the_bare_artifact_entry() {
        let path = tmp_settings("enable-keeps");
        std::fs::write(&path, REAL_USER_FILE).unwrap();
        set_artifact_enabled_at(&path, false).unwrap();
        set_artifact_enabled_at(&path, true).unwrap();

        let v = read_back(&path);
        let deny = v["permissions"]["deny"].as_array().unwrap();
        assert_eq!(
            deny,
            &vec![
                Value::String("Bash(rm -rf:*)".into()),
                Value::String("Bash(sudo:*)".into())
            ],
            "only our entry is removed, order of the user's is preserved"
        );
        assert_eq!(v["permissions"]["allow"].as_array().unwrap().len(), 3);
        cleanup(&path);
    }

    /// A scoped rule is user content: VCO neither writes nor removes it, and
    /// says so by still reporting it after an enable.
    #[test]
    fn enable_does_not_touch_a_user_authored_scoped_deny_rule() {
        let path = tmp_settings("scoped-kept");
        std::fs::write(
            &path,
            r#"{"permissions": {"deny": ["Artifact", "Artifact(*)"]}}"#,
        )
        .unwrap();
        let state = set_artifact_enabled_at(&path, true).unwrap();
        assert!(!state.deny_bare_artifact);
        assert!(state.deny_scoped_artifact, "still there, still reported");
        let v = read_back(&path);
        assert_eq!(
            v["permissions"]["deny"].as_array().unwrap(),
            &vec![Value::String("Artifact(*)".into())]
        );
        cleanup(&path);
    }

    #[test]
    fn completing_a_partially_applied_disable_adds_only_the_missing_key() {
        let path = tmp_settings("complete");
        std::fs::write(&path, r#"{"enableArtifact": false, "model": "opus"}"#).unwrap();
        let state = set_artifact_enabled_at(&path, false).unwrap();
        assert!(state.fully_disabled);
        let v = read_back(&path);
        assert_eq!(v["permissions"]["deny"][0], "Artifact");
        assert_eq!(v["model"], "opus");
        cleanup(&path);
    }

    #[test]
    fn repeat_disable_is_idempotent_and_does_not_duplicate_the_deny_entry() {
        let path = tmp_settings("idempotent");
        std::fs::write(&path, REAL_USER_FILE).unwrap();
        set_artifact_enabled_at(&path, false).unwrap();
        let after_first = std::fs::read_to_string(&path).unwrap();
        let mtime_first = std::fs::metadata(&path).unwrap().modified().unwrap();

        set_artifact_enabled_at(&path, false).unwrap();
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            after_first,
            "a no-op disable must not rewrite the file"
        );
        assert_eq!(
            std::fs::metadata(&path).unwrap().modified().unwrap(),
            mtime_first,
            "a no-op disable must not even touch the mtime"
        );
        let v = read_back(&path);
        let artifact_entries = v["permissions"]["deny"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e.as_str() == Some("Artifact"))
            .count();
        assert_eq!(artifact_entries, 1);
        cleanup(&path);
    }

    /// A write is a full re-serialisation of a file the USER owns, so it must
    /// change nothing it was not asked to change: no key dropped, no value
    /// altered, and — since v0.2.92's `preserve_order` — no key MOVED.
    ///
    /// `REAL_USER_FILE` is deliberately not in alphabetical order
    /// (`_hooks_removed_2026-05-16` sits after `features`, `model` is last),
    /// so a serialiser that sorted would fail the order assertion below.
    /// Before v0.2.92 this test was named `a_write_may_reorder_keys_…` and
    /// deliberately did NOT assert order — it pinned the defect as expected
    /// behaviour. The defect is fixed, so the assertion is the point now.
    #[test]
    fn a_write_preserves_key_order_and_never_drops_or_changes_a_value() {
        let path = tmp_settings("reorder");
        let before: Value = serde_json::from_str(REAL_USER_FILE).unwrap();
        std::fs::write(&path, REAL_USER_FILE).unwrap();

        set_artifact_enabled_at(&path, false).unwrap();
        let after = read_back(&path);

        for (key, value) in before.as_object().unwrap() {
            if key == "permissions" {
                // The only key we own a part of; asserted in detail elsewhere.
                continue;
            }
            assert_eq!(
                after.get(key),
                Some(value),
                "key {} changed or disappeared across the rewrite",
                key
            );
        }
        assert_eq!(
            after["permissions"]["allow"],
            before["permissions"]["allow"],
            "we touch deny, never allow"
        );

        // Order: every key the user had, in the order they had it, followed
        // by the one key a disable adds. `enableArtifact` is new, so it lands
        // at the end — appending is the only placement that moves nothing.
        let before_keys: Vec<&String> = before.as_object().unwrap().keys().collect();
        let after_keys: Vec<&String> = after.as_object().unwrap().keys().collect();
        let mut expected: Vec<&String> = before_keys.clone();
        let enable_key = ENABLE_ARTIFACT_KEY.to_string();
        expected.push(&enable_key);
        assert_eq!(
            after_keys, expected,
            "a write must preserve the user's key order and append only what it adds"
        );
        cleanup(&path);
    }

    /// Disable then re-enable must return the document to the user's ORIGINAL
    /// key order, not merely to the same set of keys.
    ///
    /// This is the regression pin for `Map::remove` → `shift_remove`. Under
    /// `preserve_order`, `remove` is `swap_remove`: dropping `enableArtifact`
    /// would fill its slot with the map's LAST key (`model`), so the round
    /// trip would leave the user's file permanently scrambled even though
    /// every key survived.
    #[test]
    fn a_disable_then_enable_round_trip_restores_the_original_key_order() {
        let path = tmp_settings("roundtrip-order");
        std::fs::write(&path, REAL_USER_FILE).unwrap();
        let original: Value = serde_json::from_str(REAL_USER_FILE).unwrap();

        set_artifact_enabled_at(&path, false).unwrap();
        set_artifact_enabled_at(&path, true).unwrap();
        let after = read_back(&path);

        assert_eq!(
            after.as_object().unwrap().keys().collect::<Vec<_>>(),
            original.as_object().unwrap().keys().collect::<Vec<_>>(),
            "a disable→enable round trip must leave the key order untouched"
        );
        assert_eq!(after, original, "and the document itself unchanged");
        cleanup(&path);
    }

    /// Pruning a container VCO emptied must not drag an unrelated key across
    /// the document.
    ///
    /// `permissions` sits in the MIDDLE here with a deny list holding only the
    /// entry VCO wrote, so enabling empties it and prunes both `deny` and
    /// `permissions`. With `Map::remove` (= `swap_remove`) the last key
    /// (`zLastKey`) would jump into the vacated middle slot; with
    /// `shift_remove` the survivors close up in place.
    #[test]
    fn pruning_an_emptied_container_does_not_swap_the_last_key_into_its_slot() {
        let path = tmp_settings("prune-order");
        std::fs::write(
            &path,
            r#"{
                "aFirstKey": 1,
                "enableArtifact": false,
                "permissions": {"deny": ["Artifact"]},
                "mMiddleKey": 2,
                "zLastKey": 3
            }"#,
        )
        .unwrap();

        set_artifact_enabled_at(&path, true).unwrap();
        let after = read_back(&path);

        assert_eq!(
            after.as_object().unwrap().keys().collect::<Vec<_>>(),
            vec!["aFirstKey", "mMiddleKey", "zLastKey"],
            "removals must close up in place, never swap the tail forward"
        );
        cleanup(&path);
    }

    /// A Windows-authored settings file (CRLF line endings) survives a write
    /// with its keys, values and ORDER intact.
    ///
    /// Stated honestly rather than over-promised: `to_string_pretty` emits LF,
    /// so the rewritten file is LF-terminated on every OS. That is unchanged
    /// by v0.2.92 — it has always been true of every JSON file the launcher
    /// writes — and it is a whole-file property no editor treats as data loss.
    /// What WOULD be data loss is a reordered or dropped key, and that is what
    /// this pins for CRLF input.
    #[test]
    fn a_crlf_authored_file_round_trips_with_its_key_order_intact() {
        let path = tmp_settings("crlf");
        let crlf = REAL_USER_FILE.replace('\n', "\r\n");
        assert!(crlf.contains("\r\n"), "fixture precondition: CRLF present");
        std::fs::write(&path, &crlf).unwrap();
        let before: Value = serde_json::from_str(&crlf).unwrap();

        set_artifact_enabled_at(&path, false).unwrap();
        let after = read_back(&path);

        let mut expected: Vec<String> =
            before.as_object().unwrap().keys().cloned().collect();
        expected.push(ENABLE_ARTIFACT_KEY.to_string());
        assert_eq!(
            after.as_object().unwrap().keys().cloned().collect::<Vec<_>>(),
            expected,
            "CRLF input must not disturb key order"
        );
        assert_eq!(
            after["model"], before["model"],
            "and values survive the line-ending change"
        );
        cleanup(&path);
    }

    #[test]
    fn enabling_with_no_settings_file_creates_nothing_at_all() {
        let path = tmp_settings("enable-nofile");
        let dir = path.parent().unwrap().to_path_buf();
        std::fs::remove_dir_all(&dir).unwrap();
        assert!(!dir.exists(), "precondition: not even the directory exists");

        let state = set_artifact_enabled_at(&path, true).expect("no-op must succeed");
        assert_eq!(state.artifacts_enabled, Some(true));
        assert!(
            !dir.exists(),
            "a no-op enable must not create the .claude directory, the file, or a lock"
        );
    }

    #[test]
    fn a_no_op_enable_on_a_pristine_file_writes_nothing_at_all() {
        let path = tmp_settings("noop-enable");
        std::fs::write(&path, REAL_USER_FILE).unwrap();
        set_artifact_enabled_at(&path, true).unwrap();
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            REAL_USER_FILE,
            "nothing to change means nothing is written (not even reformatted)"
        );
        assert!(
            !json_file::sibling_path(&path, BACKUP_EXT).exists(),
            "and no backup is taken for a write that never happened"
        );
        cleanup(&path);
    }

    #[test]
    fn the_first_modification_backs_up_the_users_original_once() {
        let path = tmp_settings("backup");
        std::fs::write(&path, REAL_USER_FILE).unwrap();

        assert!(read_artifact_state(&path).backup_path.is_none());
        let state = set_artifact_enabled_at(&path, false).unwrap();
        let bak = state.backup_path.expect("backup recorded in state");
        assert_eq!(
            std::fs::read_to_string(&bak).unwrap(),
            REAL_USER_FILE,
            "the backup holds the pre-VCO bytes"
        );

        // A later toggle must not overwrite the pristine copy.
        set_artifact_enabled_at(&path, true).unwrap();
        assert_eq!(std::fs::read_to_string(&bak).unwrap(), REAL_USER_FILE);
        cleanup(&path);
    }

    #[test]
    fn the_default_path_is_the_user_scope_settings_file_not_claude_json() {
        let p = user_claude_settings_json();
        assert!(p.ends_with(Path::new(".claude").join("settings.json")), "{:?}", p);
        assert!(
            !p.to_string_lossy().ends_with(".claude.json"),
            "must never target ~/.claude.json (MCP registrations)"
        );
    }
}
