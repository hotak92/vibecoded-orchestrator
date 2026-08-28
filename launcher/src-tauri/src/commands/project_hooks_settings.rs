// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Hook enforcement: the launcher's bridge to the ONE `.claude/settings.json`
//! hooks-block writer (v0.2.91, decision #27).
//!
//! ## The bug this closes
//!
//! The Hooks tab was a **full placebo**. `register_project_hook` was a pure
//! `INSERT`, `set_project_hook_enabled` a pure `UPDATE`, `unregister_project_hook`
//! a pure `DELETE` — all against `project_hooks`, a table nothing reads.
//! Claude Code's hook engine reads `<project>/.claude/settings.json` directly,
//! so unchecking a hook did not stop it firing and registering one did not make
//! it fire. Agents and skills got the v0.2.53 FS-disable enforcement contract
//! (`apply_fs_disable_agent` / `apply_fs_disable_skill`); hooks never did.
//! Evidence: `.claude/context/reviews/v0291-wave5-phase2-ux-completeness`
//! P2-B2 and `v0291-wave5-p2-project-notes` F-P2-1.
//!
//! ## The writer, and why it is Python (A>B>C, A-leg)
//!
//! Every mutation here shells out to `python -m vco_lib.hooks_settings`. That
//! module is the only code in the repo that edits a hooks block, and it is
//! Python because the settings.json SHAPE is already owned Python-side:
//! `vco_lib.project_init::_merge_settings_template_for_bundle` (and its
//! `_merge_hooks_for_bundle` helper) create and update the file on every
//! install and bundle update, and the canonical on-disk form is their output.
//! A Rust JSON writer would be a second home for that knowledge; the drift
//! that produces is already documented in `vco_lib/settings_merge.py` for the
//! install.py / project_init.py pair. Hook toggling is a user-action-triggered
//! path where a ~100 ms subprocess is invisible, so the A-leg applies with no
//! caveat.
//!
//! Interpreter resolution reuses the existing RT-4 ladder
//! (`python_resolve::resolve_python_for_vco_lib`) and the cwd-is-the-clone-root
//! rule that `embedding_catalog` documents — `vco_lib` is an in-tree namespace
//! package, so `python -m vco_lib.X` needs the clone root as cwd.
//!
//! ## Where truth lives
//!
//! `settings.json` is the truth about what runs; `project_hooks` is a mirror
//! plus the parked-entry store (`db::project_hooks_settings`). The read path
//! ([`list_project_hooks_effective`]) therefore renders from the FILE and only
//! joins the DB for metadata and for the parked (disabled) entries. When the
//! file cannot be read the view says so and the GUI disables its controls —
//! it never silently falls back to DB rows, because that fallback *is* the
//! placebo.

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use tauri::{command, State};
use tokio::time::timeout;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// Ceiling for one `vco_lib.hooks_settings` call. The work is a single small
/// JSON read/modify/write; anything past this is a stuck interpreter, and
/// hanging the GUI on it would be worse than a legible timeout error.
const HOOKS_CLI_TIMEOUT_SECS: u64 = 30;

// ═══════════════════════════════════════════════════════════════════════
// Wire types
// ═══════════════════════════════════════════════════════════════════════

/// The state of one hook, as the Hooks tab renders it.
///
/// Deliberately a three-state enum rather than a bool: "not in settings.json"
/// and "parked by VCO" are different situations for the user, and collapsing
/// them is how the tab came to lie in the first place.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HookState {
    /// Declared in `settings.json` — it runs.
    Active,
    /// Absent from `settings.json` because VCO removed it; the entry is
    /// parked in the DB and can be restored exactly.
    Disabled,
    /// A `project_hooks` row with no matching settings.json entry and nothing
    /// parked. The wiring was removed outside the launcher (hand-edit, a
    /// bundle change, another tool). Shown, but honestly labelled — it does
    /// not run, and VCO has nothing to restore.
    Orphan,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EffectiveHook {
    /// `project_hooks.id` when the launcher has a mirror row; `None` for a
    /// hook that lives in settings.json but was never scanned.
    pub id: Option<i64>,
    pub event: String,
    pub matcher: String,
    pub command: String,
    pub source: String,
    pub source_module: Option<String>,
    pub timeout_ms: Option<i64>,
    pub state: HookState,
}

/// What the Hooks tab renders. Carries the honesty flags alongside the rows.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EffectiveHooksView {
    pub hooks: Vec<EffectiveHook>,
    /// Absolute path of the file that decides what runs — named in the UI so
    /// the user knows which VCS-tracked file an edit will show up in.
    pub settings_path: String,
    /// False when settings.json could not be read or parsed. The GUI must
    /// disable its controls and show `error`; it must NOT render DB rows as
    /// though they were the truth.
    pub settings_readable: bool,
    /// Stable machine code from the Python writer (`unparseable`, `missing`,
    /// `hooks_block_malformed`, …) when `settings_readable` is false.
    pub error_code: Option<String>,
    pub error: Option<String>,
    /// Positions in the hooks block that could not be represented as rows
    /// (an inner item with no string `command`). Surfaced rather than dropped.
    pub skipped: Vec<String>,
}

// ═══════════════════════════════════════════════════════════════════════
// The Python bridge
// ═══════════════════════════════════════════════════════════════════════

/// A refusal or failure from `vco_lib.hooks_settings`, carrying its stable
/// `code` so callers (and tests) can branch on the reason.
#[derive(Debug, Clone)]
pub struct HooksCliError {
    pub code: String,
    pub message: String,
}

impl std::fmt::Display for HooksCliError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl From<HooksCliError> for String {
    fn from(e: HooksCliError) -> String {
        e.message
    }
}

fn cli_error(code: &str, message: impl Into<String>) -> HooksCliError {
    HooksCliError { code: code.to_string(), message: message.into() }
}

/// Resolve a project's folder path, failing loudly when the row is gone.
fn project_folder(db: &Db, project_id: &str) -> Result<PathBuf, HooksCliError> {
    match db.get_project(project_id) {
        Ok(Some(p)) => Ok(PathBuf::from(p.folder_path)),
        Ok(None) => Err(cli_error(
            "project_not_found",
            format!("project {} is not registered", project_id),
        )),
        Err(e) => Err(cli_error("db_error", e)),
    }
}

pub fn settings_json_path(folder: &std::path::Path) -> PathBuf {
    folder.join(".claude").join("settings.json")
}

/// Invoke `python -m vco_lib.hooks_settings <args>` and parse its single-JSON
/// stdout object.
///
/// Contract with the Python side: stdout carries exactly one JSON object and
/// nothing else, on every path including refusals. Exit 0 means the operation
/// ran; non-zero means it was refused or errored, and the JSON still carries
/// `code` + `error`. Anything that does not fit that shape (interpreter
/// missing, module not importable, a crash before the emit) is surfaced with
/// stderr attached rather than degraded into a fake success — a silent degrade
/// here would recreate the placebo.
async fn run_hooks_cli(
    db: &Db,
    project_folder: &std::path::Path,
    args: &[&str],
) -> Result<JsonValue, HooksCliError> {
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib().ok_or_else(
        || {
            cli_error(
                "no_python",
                "no Python interpreter found for vco_lib — the hooks editor \
                 cannot run. Check the orchestrator venv.",
            )
        },
    )?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m").arg("vco_lib.hooks_settings");
    for a in args {
        cmd.arg(a);
    }
    cmd.arg("--project-folder").arg(project_folder.as_os_str());

    // `vco_lib` is an in-tree namespace package, so `python -m vco_lib.X`
    // resolves via the cwd — same rule `embedding_catalog::run_discover`
    // documents. Best-effort: with no discoverable clone root the subprocess
    // fails with ModuleNotFoundError, which is reported honestly below.
    if let Some(root) = crate::commands::installer::resolve_install_root_sync(db) {
        cmd.current_dir(&root);
    }
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let output = match timeout(Duration::from_secs(HOOKS_CLI_TIMEOUT_SECS), cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            return Err(cli_error(
                "spawn_failed",
                format!("cannot run the hooks editor ({}): {}", python.display(), e),
            ))
        }
        Err(_) => {
            return Err(cli_error(
                "timeout",
                format!("the hooks editor did not finish within {HOOKS_CLI_TIMEOUT_SECS}s"),
            ))
        }
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let parsed: JsonValue = match serde_json::from_str(stdout.trim()) {
        Ok(v) => v,
        Err(e) => {
            return Err(cli_error(
                "bad_output",
                format!(
                    "the hooks editor produced unreadable output ({}). \
                     stdout: {} stderr: {}",
                    e,
                    stdout.trim(),
                    stderr.trim()
                ),
            ))
        }
    };

    if parsed.get("ok").and_then(JsonValue::as_bool) == Some(true) {
        return Ok(parsed);
    }
    let code = parsed
        .get("code")
        .and_then(JsonValue::as_str)
        .unwrap_or("unknown")
        .to_string();
    let message = parsed
        .get("error")
        .and_then(JsonValue::as_str)
        .unwrap_or("the hooks editor refused the operation")
        .to_string();
    Err(HooksCliError { code, message })
}

// ═══════════════════════════════════════════════════════════════════════
// Read
// ═══════════════════════════════════════════════════════════════════════

/// Build the Hooks tab's view: settings.json first, DB for metadata + parked.
#[command]
pub async fn list_project_hooks_effective(
    project_id: String,
    db: State<'_, Db>,
) -> Result<EffectiveHooksView, String> {
    effective_hooks_view(&db, &project_id).await
}

/// Testable core of [`list_project_hooks_effective`] (takes `&Db` instead of
/// `State<Db>`) — the `create_starter_diagram_file_with_db` precedent.
pub async fn effective_hooks_view(
    db: &Db,
    project_id: &str,
) -> Result<EffectiveHooksView, String> {
    let folder = project_folder(db, project_id)?;
    let settings_path = settings_json_path(&folder).to_string_lossy().to_string();

    // Mirror rows supply the metadata settings.json does not carry (which
    // bundle a hook came from, whether it is a paid-module hook).
    let mirror = db.list_project_hooks(project_id).unwrap_or_default();
    let meta = |event: &str, matcher: &str, command: &str| {
        mirror
            .iter()
            .find(|h| h.event == event && h.matcher == matcher && h.command == command)
            .cloned()
    };

    let listed = match run_hooks_cli(db, &folder, &["list"]).await {
        Ok(v) => v,
        Err(e) => {
            // Honest degradation: no rows, the reason, controls off. Never
            // the DB mirror dressed up as the truth.
            return Ok(EffectiveHooksView {
                hooks: Vec::new(),
                settings_path,
                settings_readable: false,
                error_code: Some(e.code),
                error: Some(e.message),
                skipped: Vec::new(),
            });
        }
    };

    let mut hooks: Vec<EffectiveHook> = Vec::new();
    let mut active_keys: Vec<(String, String, String)> = Vec::new();
    if let Some(arr) = listed.get("hooks").and_then(JsonValue::as_array) {
        for entry in arr {
            let event = entry.get("event").and_then(JsonValue::as_str).unwrap_or("");
            let matcher = entry.get("matcher").and_then(JsonValue::as_str).unwrap_or("");
            let command = entry.get("command").and_then(JsonValue::as_str).unwrap_or("");
            if event.is_empty() || command.is_empty() {
                continue;
            }
            let row = meta(event, matcher, command);
            let timeout_ms = entry
                .get("timeout_seconds")
                .and_then(JsonValue::as_i64)
                .map(|s| s.saturating_mul(1000))
                .or_else(|| row.as_ref().and_then(|r| r.timeout_ms));
            active_keys.push((event.to_string(), matcher.to_string(), command.to_string()));
            hooks.push(EffectiveHook {
                id: row.as_ref().map(|r| r.id),
                event: event.to_string(),
                matcher: matcher.to_string(),
                command: command.to_string(),
                source: row
                    .as_ref()
                    .map(|r| r.source.clone())
                    .unwrap_or_else(|| "project".to_string()),
                source_module: row.as_ref().and_then(|r| r.source_module.clone()),
                timeout_ms,
                state: HookState::Active,
            });
        }
    }

    // Parked (VCO-disabled) hooks: absent from the file BY OUR DOING, and
    // restorable. If one has somehow reappeared in the file (the user put the
    // line back by hand) the active entry above already covers it — skip the
    // duplicate rather than render the same hook twice.
    for p in db.list_parked_project_hooks(project_id).unwrap_or_default() {
        let key = (p.event.clone(), p.matcher.clone(), p.command.clone());
        if active_keys.contains(&key) {
            continue;
        }
        hooks.push(EffectiveHook {
            id: Some(p.id),
            event: p.event,
            matcher: p.matcher,
            command: p.command,
            source: p.source,
            source_module: p.source_module,
            timeout_ms: p.timeout_ms,
            state: HookState::Disabled,
        });
    }

    // Everything else in the mirror is stale: no settings.json entry, nothing
    // parked. Rendered as an orphan so the user can see (and clear) the stale
    // wiring instead of believing a hook exists that does not.
    for row in &mirror {
        let key = (row.event.clone(), row.matcher.clone(), row.command.clone());
        if active_keys.contains(&key) || hooks.iter().any(|h| h.id == Some(row.id)) {
            continue;
        }
        hooks.push(EffectiveHook {
            id: Some(row.id),
            event: row.event.clone(),
            matcher: row.matcher.clone(),
            command: row.command.clone(),
            source: row.source.clone(),
            source_module: row.source_module.clone(),
            timeout_ms: row.timeout_ms,
            state: HookState::Orphan,
        });
    }

    hooks.sort_by(|a, b| {
        a.event
            .cmp(&b.event)
            .then_with(|| a.matcher.cmp(&b.matcher))
            .then_with(|| a.command.cmp(&b.command))
    });

    let skipped = listed
        .get("skipped")
        .and_then(JsonValue::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    Ok(EffectiveHooksView {
        hooks,
        settings_path,
        settings_readable: true,
        error_code: None,
        error: None,
        skipped,
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Write
// ═══════════════════════════════════════════════════════════════════════

/// Record a hook mutation in the audit log, soft-failing with a log line.
///
/// The file edit has already happened by the time this runs, so an audit-write
/// failure must not turn a completed, correct operation into an error the user
/// sees as "it didn't work". It still has to be visible, hence the warn.
///
/// This applies to all FOUR hook-mutation ops — disable, enable, register,
/// unregister. The settings.json edit is always the operation the user asked
/// for and it has already succeeded by the time any of them calls this; the
/// audit row is a best-effort trail on top of it, never a gate on the
/// outcome. (Wave-5 review MINOR-4: register/unregister used to call
/// `db.audit(...)?` directly, so a failed audit write there surfaced as a
/// user-visible error toast for an operation that had already succeeded —
/// inconsistent with disable/enable, which always routed through this
/// helper.)
fn audit_soft(action: &str, db: &Db, project_id: &str, detail: &serde_json::Value) {
    if let Err(e) = db.audit(action, Some(project_id), None, detail) {
        tracing::warn!(
            "[vct] {} audit write failed for project {}: {}",
            action, project_id, e
        );
    }
}

/// Disable a hook: REMOVE its settings.json entry, park the removed entry.
///
/// Ordering is load-bearing. The file edit happens FIRST; only when it
/// succeeds is the entry parked. The reverse order would leave a row claiming
/// a removal that never happened — a new, quieter placebo.
pub async fn disable_hook(
    db: &Db,
    project_id: &str,
    event: &str,
    matcher: &str,
    command: &str,
) -> Result<(), HooksCliError> {
    let folder = project_folder(db, project_id)?;
    let result = run_hooks_cli(
        db,
        &folder,
        &["disable", "--event", event, "--matcher", matcher, "--command", command],
    )
    .await?;

    // Store the writer's OWN serialisation, verbatim. Do NOT read `parked`
    // (the object) and re-serialise it: `serde_json::Value` is backed by a
    // BTreeMap unless the `preserve_order` feature is enabled, so a round trip
    // through it SORTS the inner hook item's keys — `{type, command, timeout}`
    // becomes `{command, timeout, type}`, and the file restored on re-enable
    // no longer matches the original byte-for-byte. A JSON *string* value has
    // no such hazard, which is why the writer hands us one.
    let parked_json = result
        .get("parked_json")
        .and_then(JsonValue::as_str)
        .ok_or_else(|| {
            cli_error(
                "bad_output",
                "the hooks editor removed the entry but returned no parked entry",
            )
        })?;

    db.park_project_hook_entry(project_id, event, matcher, command, parked_json)
        .map_err(|e| {
            cli_error(
                "db_error",
                crate::db::project_hooks_settings::park_failure_message(parked_json, &e),
            )
        })?;
    audit_soft(
        "project_hook_disabled",
        db,
        project_id,
        &serde_json::json!({ "event": event, "matcher": matcher, "command": command }),
    );
    Ok(())
}

/// Re-enable a hook: restore the parked entry into settings.json, then clear
/// the parked column.
///
/// Refuses when nothing is parked — there would be no entry to restore, and
/// inventing one from the mirror row would lose the original's timeout /
/// `async` / position and quietly write a DIFFERENT hook than the user
/// disabled.
pub async fn enable_hook(
    db: &Db,
    project_id: &str,
    event: &str,
    matcher: &str,
    command: &str,
) -> Result<(), HooksCliError> {
    let folder = project_folder(db, project_id)?;
    let parked = db
        .get_parked_project_hook_entry(project_id, event, matcher, command)
        .map_err(|e| cli_error("db_error", e))?
        .ok_or_else(|| {
            cli_error(
                "nothing_parked",
                format!(
                    "no parked entry for `{}` under {} — VCO did not remove this \
                     hook, so it has nothing to restore. Add it with + Register.",
                    command, event
                ),
            )
        })?;

    run_hooks_cli(db, &folder, &["enable", "--entry-json", &parked]).await?;
    db.unpark_project_hook_entry(project_id, event, matcher, command)
        .map_err(|e| cli_error("db_error", e))?;
    audit_soft(
        "project_hook_enabled",
        db,
        project_id,
        &serde_json::json!({ "event": event, "matcher": matcher, "command": command }),
    );
    Ok(())
}

/// Toggle a hook's enforcement. The Hooks tab checkbox lands here.
#[command]
pub async fn set_project_hook_enabled(
    project_id: String,
    event: String,
    matcher: String,
    command: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if enabled {
        enable_hook(&db, &project_id, &event, &matcher, &command).await?;
    } else {
        disable_hook(&db, &project_id, &event, &matcher, &command).await?;
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct RegisterHookSettingsReq {
    pub event: String,
    #[serde(default)]
    pub matcher: String,
    pub command: String,
    pub timeout_seconds: Option<i64>,
    /// Create the hook script the command points at when it does not exist.
    /// Never overwrites an existing file.
    #[serde(default)]
    pub create_starter: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RegisterHookOutcome {
    /// False when the entry was already present — the register was a no-op,
    /// not a duplicate.
    pub changed: bool,
    /// Absolute path of the starter script, when one was requested.
    pub starter_path: Option<String>,
    /// True only when the starter file was actually written (false = it
    /// already existed and was left alone).
    pub starter_created: bool,
}

/// Register a hook: add the settings.json entry, mirror it, optionally seed
/// the script.
#[command]
pub async fn register_project_hook(
    project_id: String,
    req: RegisterHookSettingsReq,
    db: State<'_, Db>,
) -> Result<RegisterHookOutcome, String> {
    register_hook_entry(&db, &project_id, req).await
}

/// Testable core of [`register_project_hook`] (takes `&Db`).
pub async fn register_hook_entry(
    db: &Db,
    project_id: &str,
    req: RegisterHookSettingsReq,
) -> Result<RegisterHookOutcome, String> {
    let folder = project_folder(db, project_id)?;
    let timeout_arg = req.timeout_seconds.map(|s| s.to_string());

    let mut args: Vec<&str> = vec![
        "register",
        "--event",
        &req.event,
        "--matcher",
        &req.matcher,
        "--command",
        &req.command,
    ];
    if let Some(t) = timeout_arg.as_deref() {
        args.push("--timeout-seconds");
        args.push(t);
    }
    if req.create_starter {
        args.push("--create-starter");
    }
    let result = run_hooks_cli(&db, &folder, &args).await?;

    let changed = result.get("changed").and_then(JsonValue::as_bool).unwrap_or(false);
    let starter = result.get("starter");
    let starter_path = starter
        .and_then(|s| s.get("path"))
        .and_then(JsonValue::as_str)
        .map(str::to_string);
    let starter_created = starter
        .and_then(|s| s.get("created"))
        .and_then(JsonValue::as_bool)
        .unwrap_or(false);

    // Mirror the new wiring so the row carries metadata + so a later disable
    // has somewhere to park. `timeout` in settings.json is SECONDS; the
    // column is milliseconds (the same conversion `populate_hooks` does).
    db.register_project_hook(
        &project_id,
        &req.event,
        &req.matcher,
        &req.command,
        "project",
        None,
        req.timeout_seconds.map(|s| s.saturating_mul(1000)),
        &serde_json::json!({}),
    )?;
    // A hook the user had DISABLED and has now re-registered is in the file
    // again, so the parked entry no longer describes anything. Clearing it
    // keeps the row's story and the file's story the same — a stale parked
    // entry would render the hook as Disabled the moment the file said
    // otherwise.
    db.unpark_project_hook_entry(&project_id, &req.event, &req.matcher, &req.command)?;
    audit_soft(
        "project_hook_register",
        db,
        project_id,
        &serde_json::json!({
            "event": req.event, "matcher": req.matcher, "command": req.command
        }),
    );

    Ok(RegisterHookOutcome { changed, starter_path, starter_created })
}

/// Unregister a hook: remove its settings.json entry and its mirror row.
///
/// **Never deletes the hook script file.** Removing the wiring is not removing
/// the user's code, and the confirm copy in the GUI says so.
#[command]
pub async fn unregister_project_hook(
    project_id: String,
    event: String,
    matcher: String,
    command: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    unregister_hook_entry(&db, &project_id, &event, &matcher, &command).await
}

/// Testable core of [`unregister_project_hook`] (takes `&Db`).
pub async fn unregister_hook_entry(
    db: &Db,
    project_id: &str,
    event: &str,
    matcher: &str,
    command: &str,
) -> Result<(), String> {
    let folder = project_folder(db, project_id)?;

    // Always ask the writer to remove the entry, whatever the row says. A
    // parked hook is normally absent from the file already, and an orphan
    // always is — but the user may have put either line back by hand, and
    // trusting the row over the file is the habit this work package exists to
    // break. `not_found` then just means there was nothing to remove.
    match run_hooks_cli(
        &db,
        &folder,
        &["unregister", "--event", &event, "--matcher", &matcher, "--command", &command],
    )
    .await
    {
        Ok(_) => {}
        // Nothing in the file to remove — clearing the row is still the
        // requested outcome, so this is a no-op, not a failure. Every OTHER
        // refusal (unparseable file, symlink, write failure) must stop the
        // delete, so the row never outlives a file we could not edit.
        Err(e) if e.code == "not_found" => {}
        Err(e) => return Err(e.message),
    }

    db.delete_project_hook_by_key(&project_id, &event, &matcher, &command)?;
    audit_soft(
        "project_hook_unregister",
        db,
        project_id,
        &serde_json::json!({
            "event": event, "matcher": matcher, "command": command,
            "script_file_deleted": false
        }),
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use vct_launcher_core::db::models::ProjectHost;

    /// The repo root, from this crate's manifest dir (`launcher/src-tauri`).
    fn repo_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repo root")
            .to_path_buf()
    }

    const SETTINGS_JSON: &str = r#"{
  "env": {
    "KG_COLLECTION": "Fixture_KnowledgeGraph"
  },
  "permissions": {
    "allow": [],
    "deny": []
  },
  "userCustomKey": "keep me",
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit(*)",
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/post-file-edit.sh",
            "timeout": 30
          },
          {
            "type": "command",
            "command": "bash .claude/hooks/post-tool-security.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/cost-tracker.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
"#;

    struct Fixture {
        db: Db,
        pid: String,
        _td: tempfile::TempDir,
        settings: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            Self::with_settings(SETTINGS_JSON)
        }

        fn with_settings(body: &str) -> Self {
            let td = tempfile::TempDir::new().unwrap();
            let claude = td.path().join(".claude");
            std::fs::create_dir_all(&claude).unwrap();
            let settings = claude.join("settings.json");
            std::fs::write(&settings, body).unwrap();

            let db = Db::open_in_memory().unwrap();
            let pid = uuid::Uuid::new_v4().to_string();
            db.insert_project(
                &pid,
                "HooksFixture",
                &td.path().to_string_lossy(),
                ProjectHost::Base,
                "hooksfixture",
            )
            .unwrap();
            // `run_hooks_cli` needs cwd == the orchestrator clone root so
            // `python -m vco_lib.hooks_settings` resolves the in-tree
            // namespace package. Seed the cached install path rather than
            // depending on where cargo happens to run the test from.
            db.app_state_set("install_path", &repo_root().to_string_lossy())
                .unwrap();
            Fixture { db, pid, _td: td, settings }
        }

        fn raw(&self) -> String {
            std::fs::read_to_string(&self.settings).unwrap()
        }

        fn json(&self) -> JsonValue {
            serde_json::from_str(&self.raw()).unwrap()
        }

        fn commands_under(&self, event: &str) -> Vec<String> {
            self.json()
                .get("hooks")
                .and_then(|h| h.get(event))
                .and_then(JsonValue::as_array)
                .map(|groups| {
                    groups
                        .iter()
                        .filter_map(|g| g.get("hooks").and_then(JsonValue::as_array))
                        .flatten()
                        .filter_map(|h| {
                            h.get("command").and_then(JsonValue::as_str).map(str::to_string)
                        })
                        .collect()
                })
                .unwrap_or_default()
        }
    }

    // ─── The placebo red-proof ──────────────────────────────────────────
    //
    // This is the test that fails on de2e530a. Pre-fix, disabling a hook was
    // `UPDATE project_hooks SET enabled = 0` and settings.json was never
    // opened, so the assertion below ("the entry is gone from the file")
    // could not hold no matter what the checkbox showed.

    #[tokio::test(flavor = "multi_thread")]
    async fn disabling_a_hook_removes_its_entry_from_settings_json() {
        let f = Fixture::new();
        let before = f.raw();

        disable_hook(
            &f.db,
            &f.pid,
            "PostToolUse",
            "Edit(*)",
            "bash .claude/hooks/post-tool-security.sh",
        )
        .await
        .expect("disable must succeed");

        assert_ne!(f.raw(), before, "settings.json MUST change — this is the fix");
        assert_eq!(
            f.commands_under("PostToolUse"),
            vec!["bash .claude/hooks/post-file-edit.sh".to_string()],
            "only the disabled hook is gone; its sibling stays"
        );
        // Everything the user owns survives.
        let after = f.json();
        assert_eq!(after["userCustomKey"], "keep me");
        assert_eq!(after["env"]["KG_COLLECTION"], "Fixture_KnowledgeGraph");
        // The removed entry is parked, so re-enable has something exact to
        // restore.
        let parked = f
            .db
            .get_parked_project_hook_entry(
                &f.pid,
                "PostToolUse",
                "Edit(*)",
                "bash .claude/hooks/post-tool-security.sh",
            )
            .unwrap();
        assert!(parked.is_some(), "the removed entry must be parked");
    }

    /// The pre-v0.2.91 mechanism, run verbatim, changes nothing that matters.
    ///
    /// `set_project_hook_enabled` used to be exactly the two lines below. This
    /// test asserts what that did — flip a DB flag, leave settings.json
    /// untouched — so the reason the enforcement path exists stays pinned in
    /// the tree. It is also the shape of the red-proof: on `de2e530a` the
    /// assertion in `disabling_a_hook_removes_its_entry_from_settings_json`
    /// ("settings.json MUST change") could not hold, because this is all the
    /// toggle ever did.
    #[tokio::test(flavor = "multi_thread")]
    async fn the_pre_v0291_db_only_toggle_is_provably_inert() {
        let f = Fixture::new();
        let before = f.raw();
        let row = f
            .db
            .register_project_hook(
                &f.pid,
                "Stop",
                "",
                "bash .claude/hooks/cost-tracker.sh",
                "project",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();

        f.db.set_project_hook_enabled(row.id, false).unwrap();

        assert!(!f.db.list_project_hooks(&f.pid).unwrap()[0].enabled, "the flag flipped");
        assert_eq!(
            f.raw(),
            before,
            "…and settings.json is untouched, so Claude Code still runs the hook"
        );
        assert_eq!(
            f.commands_under("Stop"),
            vec!["bash .claude/hooks/cost-tracker.sh".to_string()],
            "the entry the harness reads is still there — this is the placebo"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn re_enabling_restores_the_file_byte_for_byte() {
        let f = Fixture::new();
        let before = f.raw();

        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();
        assert!(f.commands_under("Stop").is_empty());

        enable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .expect("enable must succeed");

        assert_eq!(f.raw(), before, "re-enable restores the exact original bytes");
        assert_eq!(
            f.db.get_parked_project_hook_entry(
                &f.pid,
                "Stop",
                "",
                "bash .claude/hooks/cost-tracker.sh"
            )
            .unwrap(),
            None,
            "the parked entry is cleared once it is back in the file"
        );
    }

    // ─── Refusals: act vs leave-alone ───────────────────────────────────

    #[tokio::test(flavor = "multi_thread")]
    async fn enable_refuses_when_nothing_is_parked() {
        let f = Fixture::new();
        let before = f.raw();

        let err = enable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .expect_err("nothing parked → refuse");
        assert_eq!(err.code, "nothing_parked");
        assert_eq!(f.raw(), before, "a refused enable writes nothing");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn disable_on_an_unparseable_settings_json_refuses_without_clobbering() {
        let broken = "{ \"hooks\": { \"Stop\": [ ,,, }";
        let f = Fixture::with_settings(broken);

        let err = disable_hook(&f.db, &f.pid, "Stop", "", "anything")
            .await
            .expect_err("unparseable → refuse");
        assert_eq!(err.code, "unparseable");
        assert_eq!(f.raw(), broken, "the user's broken file is left exactly as-is");
        assert!(
            f.db.list_parked_project_hooks(&f.pid).unwrap().is_empty(),
            "a failed file edit must NOT leave a row claiming a removal happened"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn disable_of_a_hook_that_is_not_declared_refuses() {
        let f = Fixture::new();
        let before = f.raw();
        let err = disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/nope.sh")
            .await
            .expect_err("not declared → refuse");
        assert_eq!(err.code, "not_found");
        assert_eq!(f.raw(), before);
    }

    // ─── register / unregister ──────────────────────────────────────────

    #[tokio::test(flavor = "multi_thread")]
    async fn register_wires_the_entry_and_seeds_the_starter_script() {
        let f = Fixture::new();
        let folder = f._td.path().to_path_buf();

        let result = run_hooks_cli(
            &f.db,
            &folder,
            &[
                "register",
                "--event",
                "SessionEnd",
                "--matcher",
                "",
                "--command",
                "bash .claude/hooks/brand-new.sh",
                "--timeout-seconds",
                "9",
                "--create-starter",
            ],
        )
        .await
        .expect("register must succeed");

        assert_eq!(result["changed"], true);
        assert_eq!(result["starter"]["created"], true);
        assert!(folder.join(".claude/hooks/brand-new.sh").is_file());
        assert_eq!(
            f.commands_under("SessionEnd"),
            vec!["bash .claude/hooks/brand-new.sh".to_string()]
        );
        assert_eq!(f.json()["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 9);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn register_never_overwrites_an_existing_hook_script() {
        let f = Fixture::new();
        let folder = f._td.path().to_path_buf();
        let script = folder.join(".claude/hooks/mine.sh");
        std::fs::create_dir_all(script.parent().unwrap()).unwrap();
        std::fs::write(&script, "MY CONTENT\n").unwrap();

        let result = run_hooks_cli(
            &f.db,
            &folder,
            &[
                "register",
                "--event",
                "Stop",
                "--matcher",
                "",
                "--command",
                "bash .claude/hooks/mine.sh",
                "--create-starter",
            ],
        )
        .await
        .unwrap();

        assert_eq!(result["starter"]["created"], false);
        assert_eq!(std::fs::read_to_string(&script).unwrap(), "MY CONTENT\n");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn unregister_removes_the_entry_and_never_the_script_file() {
        let f = Fixture::new();
        let folder = f._td.path().to_path_buf();
        let script = folder.join(".claude/hooks/cost-tracker.sh");
        std::fs::create_dir_all(script.parent().unwrap()).unwrap();
        std::fs::write(&script, "#!/usr/bin/env bash\n").unwrap();

        unregister_hook_entry(
            &f.db,
            &f.pid,
            "Stop",
            "",
            "bash .claude/hooks/cost-tracker.sh",
        )
        .await
        .unwrap();

        assert!(f.commands_under("Stop").is_empty(), "the wiring is gone");
        assert!(script.is_file(), "the user's script file is NOT deleted");
        assert!(
            f.db.list_project_hooks(&f.pid).unwrap().is_empty(),
            "the mirror row goes too"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn unregister_of_a_stale_row_clears_it_without_erroring() {
        let f = Fixture::new();
        f.db.register_project_hook(
            &f.pid,
            "PreToolUse",
            "*",
            "bash .claude/hooks/ghost.sh",
            "project",
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();
        let before = f.raw();

        unregister_hook_entry(&f.db, &f.pid, "PreToolUse", "*", "bash .claude/hooks/ghost.sh")
            .await
            .expect("a row with no file entry still clears cleanly");

        assert!(f.db.list_project_hooks(&f.pid).unwrap().is_empty());
        assert_eq!(f.raw(), before, "and settings.json is untouched");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn unregister_removes_a_parked_hook_the_user_restored_by_hand() {
        // The row says "parked", the FILE says the hook is back. Trusting the
        // row here would delete the record and leave the hook running — the
        // exact habit this work package exists to break.
        let f = Fixture::new();
        let before = f.raw();
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();
        std::fs::write(&f.settings, &before).unwrap();

        unregister_hook_entry(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();

        assert!(
            f.commands_under("Stop").is_empty(),
            "the hand-restored entry is removed from the file too"
        );
        assert!(f.db.list_parked_project_hooks(&f.pid).unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn re_registering_a_parked_hook_clears_the_stale_parked_entry() {
        let f = Fixture::new();
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();
        assert_eq!(f.db.list_parked_project_hooks(&f.pid).unwrap().len(), 1);

        register_hook_entry(
            &f.db,
            &f.pid,
            RegisterHookSettingsReq {
                event: "Stop".into(),
                matcher: String::new(),
                command: "bash .claude/hooks/cost-tracker.sh".into(),
                timeout_seconds: Some(5),
                create_starter: false,
            },
        )
        .await
        .unwrap();

        assert_eq!(
            f.commands_under("Stop"),
            vec!["bash .claude/hooks/cost-tracker.sh".to_string()],
            "the hook is back in the file"
        );
        assert!(
            f.db.list_parked_project_hooks(&f.pid).unwrap().is_empty(),
            "so the parked entry, which no longer describes anything, is cleared"
        );
        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let row = view
            .hooks
            .iter()
            .find(|h| h.command == "bash .claude/hooks/cost-tracker.sh")
            .unwrap();
        assert_eq!(row.state, HookState::Active);
    }

    // ─── The effective view ─────────────────────────────────────────────

    #[tokio::test(flavor = "multi_thread")]
    async fn the_view_reports_settings_json_not_the_mirror() {
        let f = Fixture::new();
        // A mirror row for a hook the FILE does not declare. Pre-v0.2.91 the
        // tab rendered exactly this row as a live, toggleable hook.
        f.db.register_project_hook(
            &f.pid,
            "PreToolUse",
            "*",
            "bash .claude/hooks/ghost.sh",
            "project",
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        assert!(view.settings_readable);
        let ghost = view
            .hooks
            .iter()
            .find(|h| h.command == "bash .claude/hooks/ghost.sh")
            .expect("the stale row is still shown");
        assert_eq!(
            ghost.state,
            HookState::Orphan,
            "a row the file does not declare is an ORPHAN, never Active"
        );
        assert_eq!(
            view.hooks
                .iter()
                .filter(|h| h.state == HookState::Active)
                .count(),
            3,
            "exactly the three commands settings.json declares"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_disabled_hook_shows_as_disabled_not_missing() {
        let f = Fixture::new();
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let row = view
            .hooks
            .iter()
            .find(|h| h.command == "bash .claude/hooks/cost-tracker.sh")
            .expect("a disabled hook is still listed");
        assert_eq!(row.state, HookState::Disabled);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn an_unreadable_settings_json_degrades_honestly_with_no_rows() {
        let f = Fixture::with_settings("{ not json");
        // A mirror row exists — the pre-fix code would have rendered it.
        f.db.register_project_hook(
            &f.pid,
            "Stop",
            "",
            "bash .claude/hooks/x.sh",
            "project",
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        assert!(!view.settings_readable);
        assert_eq!(view.error_code.as_deref(), Some("unparseable"));
        assert!(
            view.hooks.is_empty(),
            "never fall back to DB rows — that fallback IS the placebo"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_hook_restored_by_hand_is_not_listed_twice() {
        let f = Fixture::new();
        let before = f.raw();
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
            .await
            .unwrap();
        // The user puts the line back themselves; the parked row survives.
        std::fs::write(&f.settings, &before).unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let rows: Vec<_> = view
            .hooks
            .iter()
            .filter(|h| h.command == "bash .claude/hooks/cost-tracker.sh")
            .collect();
        assert_eq!(rows.len(), 1, "one row, not one per source of truth");
        assert_eq!(rows[0].state, HookState::Active, "the FILE decides");
    }
}
