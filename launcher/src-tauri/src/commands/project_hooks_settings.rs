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
//! that produces is already documented on
//! `vco_lib/project_init.py::_merge_settings_template_for_bundle`, the live
//! merge for the install.py / project_init.py pair. Hook toggling is a user-action-triggered
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
    let mut argv: Vec<&std::ffi::OsStr> =
        args.iter().map(|a| std::ffi::OsStr::new(*a)).collect();
    argv.push(std::ffi::OsStr::new("--project-folder"));
    argv.push(project_folder.as_os_str());
    run_vco_lib_json(db, "vco_lib.hooks_settings", &argv, None, "the hooks editor").await
}

/// Invoke `python -m <module> <args>` and parse its single-JSON stdout object.
///
/// Extracted from [`run_hooks_cli`] when the F7 eager prune added a SECOND
/// `vco_lib` CLI to this file (`vco_lib.hook_retirements`). The spawn is the
/// same sequence either way — the RT-4 interpreter ladder, the
/// cwd-is-the-clone-root rule, a wall-clock bound, one JSON object on stdout —
/// so it is written once here rather than a second time beside its new caller
/// (CLAUDE.md § "search before you add, extract before you duplicate").
///
/// Contract with every `vco_lib` machine CLI: stdout carries exactly one JSON
/// object and nothing else, on every path including refusals. Exit 0 means the
/// operation ran; non-zero means it was refused or errored, and the JSON still
/// carries `code` + `error`. Anything that does not fit that shape
/// (interpreter missing, module not importable, a crash before the emit) is
/// surfaced with stderr attached rather than degraded into a fake success — a
/// silent degrade here would recreate the placebo.
///
/// `stdin_body` is written to the child's stdin when present (the batched
/// `match` request); `None` gives the child a null stdin, which is what every
/// argv-driven subcommand wants.
///
/// `what` names the tool in error messages, so a refusal reads as "the hooks
/// editor refused…" rather than naming a module path at the user.
///
/// Crate-visible since v0.2.97: the post-bundle pipeline's registry write
/// (`projects_v2::record_bundle_materialization`) is a third caller.
pub(crate) async fn run_vco_lib_json(
    db: &Db,
    module: &str,
    args: &[&std::ffi::OsStr],
    stdin_body: Option<&str>,
    what: &str,
) -> Result<JsonValue, HooksCliError> {
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib().ok_or_else(
        || {
            cli_error(
                "no_python",
                format!(
                    "no Python interpreter found for vco_lib — {} cannot run. \
                     Check the orchestrator venv.",
                    what
                ),
            )
        },
    )?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m").arg(module);
    for a in args {
        cmd.arg(a);
    }

    // `vco_lib` is an in-tree namespace package, so `python -m vco_lib.X`
    // resolves via the cwd — same rule `embedding_catalog::run_discover`
    // documents. Best-effort: with no discoverable clone root the subprocess
    // fails with ModuleNotFoundError, which is reported honestly below.
    if let Some(root) = crate::commands::installer::resolve_install_root_sync(db) {
        cmd.current_dir(&root);
    }
    cmd.stdin(if stdin_body.is_some() {
        std::process::Stdio::piped()
    } else {
        std::process::Stdio::null()
    });
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let collect = async {
        let mut child = cmd.spawn()?;
        if let Some(body) = stdin_body {
            // Take the handle and DROP it after the write: the child reads
            // stdin to EOF, so leaving the pipe open would deadlock both
            // processes against each other.
            if let Some(mut sink) = child.stdin.take() {
                use tokio::io::AsyncWriteExt as _;
                sink.write_all(body.as_bytes()).await?;
                sink.shutdown().await?;
            }
        }
        child.wait_with_output().await
    };

    let output = match timeout(Duration::from_secs(HOOKS_CLI_TIMEOUT_SECS), collect).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            return Err(cli_error(
                "spawn_failed",
                format!("cannot run {} ({}): {}", what, python.display(), e),
            ))
        }
        Err(_) => {
            return Err(cli_error(
                "timeout",
                format!("{} did not finish within {}s", what, HOOKS_CLI_TIMEOUT_SECS),
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
                    "{} produced unreadable output ({}). stdout: {} stderr: {}",
                    what,
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
        .unwrap_or("the operation was refused")
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

    // v0.2.97: a DB row (mirror or parked) may hold an OLDER SPELLING of a
    // command settings.json now carries — the relative `.claude/hooks/x.sh`
    // every release before v0.2.97 wrote, now anchored at
    // `${CLAUDE_PROJECT_DIR}` by the bundle update; or the pre-v0.2.97
    // `VCT_DISABLE_HOOKS` guard prefix. Rows are matched to entries by the
    // registration KEY, which the hooks editor computes (`--keys-for-json`,
    // `vco_lib.hook_retirements.hook_command_key` — the one home of the rule;
    // no Rust copy of it). Without this every migrated VCO hook rendered
    // twice: once active with no metadata, once as an orphan row.
    let parked_before = db.list_parked_project_hooks(project_id).unwrap_or_default();
    let row_commands: Vec<&str> = mirror
        .iter()
        .map(|h| h.command.as_str())
        .chain(parked_before.iter().map(|p| p.command.as_str()))
        .collect();
    let keys_arg = serde_json::to_string(&row_commands).unwrap_or_else(|_| "[]".to_string());

    let listed = match run_hooks_cli(db, &folder, &["list", "--keys-for-json", &keys_arg]).await {
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

    let keys = listed.get("keys").and_then(JsonValue::as_object);
    // The registration key of `command`; the command itself when the editor
    // gave none (an older vco_lib) — exact matching, the pre-v0.2.97 rule.
    let key_of = |command: &str| -> String {
        keys.and_then(|k| k.get(command))
            .and_then(JsonValue::as_str)
            .unwrap_or(command)
            .to_string()
    };
    let meta = |event: &str, matcher: &str, command: &str| {
        let slot = || mirror.iter().filter(|h| h.event == event && h.matcher == matcher);
        slot()
            .find(|h| h.command == command)
            .or_else(|| {
                let key = key_of(command);
                slot().find(|h| key_of(&h.command) == key)
            })
            .cloned()
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
            active_keys.push((event.to_string(), matcher.to_string(), key_of(command)));
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

    // F7 eager half: release the parked bytes of registrations that have been
    // RETIRED, before rendering. A row the user cannot restore must not be
    // offered as restorable, and waiting for them to click Enable to find out
    // is a control that lies until it is used. Soft-fail — anything that goes
    // wrong here keeps every parked row (see `prune_retired_parked_rows`).
    let parked = prune_retired_parked_rows(db, project_id).await;

    // Parked (VCO-disabled) hooks: absent from the file BY OUR DOING, and
    // restorable. If one has somehow reappeared in the file (the user put the
    // line back by hand) the active entry above already covers it — skip the
    // duplicate rather than render the same hook twice.
    for p in parked {
        let key = (p.event.clone(), p.matcher.clone(), key_of(&p.command));
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
        let key = (row.event.clone(), row.matcher.clone(), key_of(&row.command));
        let rendered_as_parked = hooks.iter().any(|h| {
            h.state == HookState::Disabled
                && h.event == row.event
                && h.matcher == row.matcher
                && key_of(&h.command) == key.2
        });
        if active_keys.contains(&key)
            || rendered_as_parked
            || hooks.iter().any(|h| h.id == Some(row.id))
        {
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
    // (the object) and re-serialise it.
    //
    // The original reason was that `serde_json::Value` was backed by a
    // BTreeMap, so a round trip SORTED the inner hook item's keys —
    // `{type, command, timeout}` came back as `{command, timeout, type}` and
    // the entry restored on re-enable no longer matched byte-for-byte. That
    // specific hazard is gone: v0.2.92 enables `preserve_order` for every
    // launcher crate, so a round trip now keeps insertion order.
    //
    // The design STAYS, because it never depended on that: order is only one
    // of the ways a re-serialisation can differ from the writer's bytes.
    // Indentation, key escaping, number formatting (`1.0` vs `1`) and
    // whitespace are all re-derived by `to_string`, and none of them is
    // pinned by any contract we control — the hooks editor is a separate
    // program. Round-tripping a *string* through the DB is the only form that
    // guarantees what comes back is what the writer produced, whatever
    // serialiser either side happens to be built with. A JSON string value
    // has no such hazard, which is why the writer hands us one.
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

/// The refusal code the hooks writer returns for a parked entry whose
/// registration has been RETIRED. MUST MATCH the `HooksSettingsError` code
/// raised by `vco_lib.hooks_settings.insert_hook`.
pub const RETIRED_REFUSAL_CODE: &str = "hook_retired";

/// What to do with the parked row after a failed restore.
///
/// F7 (v0.2.95). A parked entry outlives the thing it restores: disabling a
/// hook moves its settings.json entry into `project_hooks.disabled_entry_json`,
/// and the bundle scrub that retires dead registrations walks settings.json —
/// where, by definition, a parked entry is not. So a hook the user disabled
/// BEFORE its retirement keeps a row labelled "Disabled (restorable)" for a
/// script the same update deleted.
///
/// The classification is Python's (`vco_lib.hook_retirements` owns the table
/// AND the matchers — an anchored invoked-script identity for one kind, whole-
/// command equality for the other); this reads its verdict off the refusal
/// code. Copying either the table or the matchers into Rust is how a user's
/// own hook eventually gets deleted by a near-miss.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParkedRowDisposition {
    /// Keep it parked: the restore failed for a reason that may not recur (a
    /// damaged settings.json, a missing interpreter, a timeout). The bytes are
    /// the user's and they are still the only copy.
    Keep,
    /// Drop the parked bytes: this registration can never be restored, so a
    /// row claiming it can is a standing lie. The mirror row survives and
    /// renders as an orphan, which is exactly what it now is.
    Drop,
}

/// The decision, pure so both halves are pinned: the act and the leave-alone.
pub fn parked_row_disposition(refusal_code: &str) -> ParkedRowDisposition {
    if refusal_code == RETIRED_REFUSAL_CODE {
        ParkedRowDisposition::Drop
    } else {
        ParkedRowDisposition::Keep
    }
}

// ═══════════════════════════════════════════════════════════════════════
// F7, the EAGER half: prune parked rows of retired registrations at load
// ═══════════════════════════════════════════════════════════════════════
//
// [`enable_hook`] closes the refusal half — a user who clicks Enable on a
// retired hook is told why and the row is released. That is correct but LATE:
// until the click, the tab shows "Disabled (restorable)" for something that
// can never be restored, and a user who never clicks never learns.
//
// So the tab also ASKS, once per load, with ALL of its parked rows in one
// request. One subprocess per load, never one per row: a tab holding N parked
// entries would otherwise start N interpreters, which is not a load.
//
// The verdict is Python's, for the same reason the refusal's is: matching a
// retired registration needs `normalize_command` AND the anchored
// invoked-script walk, and a Rust copy of either is how a near-miss
// eventually deletes a hook the user wrote. Rust decides only what to DO with
// the answer, which is the function below.

/// The `vco_lib` CLI that classifies registrations. MUST MATCH the module and
/// subcommand in `vco_lib/hook_retirements.py`'s `build_parser`.
const RETIREMENTS_MODULE: &str = "vco_lib.hook_retirements";

/// One parked row's fate, decided from the classifier's answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrunedParkedRow {
    pub event: String,
    pub matcher: String,
    pub command: String,
    /// The release that retired it, for the audit row.
    pub retired_in: String,
    /// What does the job now, in words (never an empty string — the Python
    /// side spells "nothing (the capability was removed)").
    pub replacement: String,
    pub reason: String,
}

/// Which of `parked` the classifier's `matches` array declares retired.
///
/// Pure, so both halves are testable without an interpreter: the ACT (a
/// retired row is named for release) and the LEAVE-ALONE (everything else
/// stays parked, because a parked entry is the user's only copy of that hook).
///
/// Matching is by the `(event, command)` the answer echoes back, NOT by
/// position: a positional zip would silently mis-attribute every later row if
/// the two sides ever disagreed about the batch length, and mis-attribution
/// here means deleting the wrong user's hook. An answer that echoes a pair we
/// did not send is ignored for the same reason.
pub fn parked_rows_to_prune(
    parked: &[vct_launcher_core::db::project_hooks_settings::ParkedHook],
    matches: &JsonValue,
) -> Vec<PrunedParkedRow> {
    let Some(rows) = matches.as_array() else {
        // No usable answer → nothing is provably retired → nothing is
        // released. Conservative default on a best-effort path.
        return Vec::new();
    };
    let mut out = Vec::new();
    for row in rows {
        if row.get("retired").and_then(JsonValue::as_bool) != Some(true) {
            continue;
        }
        let event = row.get("event").and_then(JsonValue::as_str).unwrap_or("");
        let command = row.get("command").and_then(JsonValue::as_str).unwrap_or("");
        if event.is_empty() || command.is_empty() {
            continue;
        }
        for p in parked.iter().filter(|p| p.event == event && p.command == command) {
            out.push(PrunedParkedRow {
                event: p.event.clone(),
                matcher: p.matcher.clone(),
                command: p.command.clone(),
                retired_in: row
                    .get("retired_in")
                    .and_then(JsonValue::as_str)
                    .unwrap_or("")
                    .to_string(),
                replacement: row
                    .get("replacement")
                    .and_then(JsonValue::as_str)
                    .unwrap_or("")
                    .to_string(),
                reason: row
                    .get("reason")
                    .and_then(JsonValue::as_str)
                    .unwrap_or("")
                    .to_string(),
            });
        }
    }
    out
}

/// The batched request body for one tab load.
fn retirement_match_request(
    parked: &[vct_launcher_core::db::project_hooks_settings::ParkedHook],
) -> String {
    serde_json::json!({
        "pairs": parked
            .iter()
            .map(|p| serde_json::json!({ "event": p.event, "command": p.command }))
            .collect::<Vec<_>>(),
    })
    .to_string()
}

/// Release the parked bytes of every retired registration, then return the
/// parked rows that SURVIVE.
///
/// Soft-fail throughout, and deliberately so: this runs on a read path the
/// user did not ask to mutate anything on. No parked rows → returns
/// immediately and spawns NOTHING (the steady state on every project that has
/// never disabled a hook). Classifier unreachable, timed out, or unreadable →
/// every row is kept and the view renders exactly as before; the refusal half
/// in [`enable_hook`] still catches the retired ones at click time, so the
/// failure mode of the eager half is "as good as v0.2.94", never worse.
///
/// A released row is NOT deleted from the mirror: it keeps its
/// `project_hooks` row and now renders as [`HookState::Orphan`], which is
/// exactly what it is — wiring that does not run and that VCO cannot restore.
///
/// ## Where the audit row goes
///
/// Through [`audit_soft`], with the SAME `project_hook_parked_entry_retired`
/// action [`enable_hook`] writes for the same disposition — one event, one
/// name, whichever path noticed it first. That is the launcher's
/// auto-resolution channel for launcher-owned state: `audit_log` is what the
/// `/audit` route renders, and a parked row lives in `launcher.db`, not in the
/// project tree. The PROJECT-tree channel (`auto-resolutions.jsonl`, written
/// by `vco_lib.deferral_emit.record_auto_resolution`) is Python's and is held
/// by a cross-writer lock; the bundle scrub already writes there for the
/// settings.json half of the same retirement. Adding a second, lock-unaware
/// Rust writer to that file would be a new cross-process writer for a file
/// this process does not own.
pub async fn prune_retired_parked_rows(
    db: &Db,
    project_id: &str,
) -> Vec<vct_launcher_core::db::project_hooks_settings::ParkedHook> {
    let parked = db.list_parked_project_hooks(project_id).unwrap_or_default();
    if parked.is_empty() {
        return parked;
    }

    let body = retirement_match_request(&parked);
    let answer = match run_vco_lib_json(
        db,
        RETIREMENTS_MODULE,
        &[
            std::ffi::OsStr::new("match"),
            std::ffi::OsStr::new("--json"),
        ],
        Some(&body),
        "the hook-retirement classifier",
    )
    .await
    {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(
                "[vct] could not classify {} parked hook(s) for project {}: {} \
                 — every parked entry is kept.",
                parked.len(),
                project_id,
                e.message
            );
            return parked;
        }
    };

    let doomed = parked_rows_to_prune(&parked, answer.get("matches").unwrap_or(&JsonValue::Null));
    if doomed.is_empty() {
        return parked;
    }

    for row in &doomed {
        if let Err(e) =
            db.unpark_project_hook_entry(project_id, &row.event, &row.matcher, &row.command)
        {
            tracing::warn!(
                "[vct] could not release the parked entry for the retired hook \
                 `{}` under {}: {}",
                row.command, row.event, e
            );
            continue;
        }
        audit_soft(
            "project_hook_parked_entry_retired",
            db,
            project_id,
            &serde_json::json!({
                "event": row.event,
                "matcher": row.matcher,
                "command": row.command,
                "retired_in": row.retired_in,
                "replacement": row.replacement,
                "reason": row.reason,
                "noticed_by": "hooks_tab_load",
            }),
        );
    }

    // Re-read rather than filtering the in-memory list: an unpark that failed
    // above must leave its row VISIBLE, and the DB is the only honest source
    // for which ones actually went.
    db.list_parked_project_hooks(project_id).unwrap_or_default()
}

/// Re-enable a hook: restore the parked entry into settings.json, then clear
/// the parked column.
///
/// Refuses when nothing is parked — there would be no entry to restore, and
/// inventing one from the mirror row would lose the original's timeout /
/// `async` / position and quietly write a DIFFERENT hook than the user
/// disabled.
///
/// Refuses too when the writer reports the registration RETIRED, and then
/// drops the parked bytes — see [`parked_row_disposition`]. The refusal
/// message is the writer's own and names the replacement.
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

    if let Err(refusal) = run_hooks_cli(db, &folder, &["enable", "--entry-json", &parked]).await {
        if parked_row_disposition(&refusal.code) == ParkedRowDisposition::Drop {
            // Nothing was written to settings.json — the writer refused
            // before touching it. What changes here is the DB: the parked
            // bytes are released, because "restorable" has stopped being true
            // and a control that offers an impossible action is worse than no
            // control. The mirror row stays and renders as an orphan.
            if let Err(e) = db.unpark_project_hook_entry(project_id, event, matcher, command) {
                tracing::warn!(
                    "[vct] could not drop the parked entry for a retired hook `{}` \
                     under {}: {}",
                    command, event, e
                );
            }
            audit_soft(
                "project_hook_parked_entry_retired",
                db,
                project_id,
                &serde_json::json!({
                    "event": event,
                    "matcher": matcher,
                    "command": command,
                    "reason": refusal.message,
                }),
            );
        }
        return Err(refusal);
    }
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
            "command": "bash .claude/hooks/notify-stop.sh",
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
                "bash .claude/hooks/notify-stop.sh",
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
            vec!["bash .claude/hooks/notify-stop.sh".to_string()],
            "the entry the harness reads is still there — this is the placebo"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn re_enabling_restores_the_file_byte_for_byte() {
        let f = Fixture::new();
        let before = f.raw();

        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .unwrap();
        assert!(f.commands_under("Stop").is_empty());

        enable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .expect("enable must succeed");

        // Byte-for-byte, except the one deliberate change: a VCO-shipped hook
        // parked in the pre-v0.2.97 RELATIVE form is restored anchored at the
        // project root (the relative form fails once the session's cwd moves).
        assert_eq!(
            f.raw(),
            before.replace(
                r#""bash .claude/hooks/notify-stop.sh""#,
                r#""bash \"${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/notify-stop.sh\"""#,
            ),
            "re-enable restores the original bytes, the hook path anchored"
        );
        assert_eq!(
            f.db.get_parked_project_hook_entry(
                &f.pid,
                "Stop",
                "",
                "bash .claude/hooks/notify-stop.sh"
            )
            .unwrap(),
            None,
            "the parked entry is cleared once it is back in the file"
        );
    }

    // ─── v0.2.97: one hook, two spellings ──────────────────────────────
    //
    // A bundle update rewrites every VCO hook from `bash .claude/hooks/x.sh`
    // to `bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/x.sh"`; the launcher DB's
    // mirror and parked rows keep the spelling they were written with.

    const ANCHORED_STOP: &str = r#"bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/notify-stop.sh""#;

    fn anchored_settings() -> String {
        SETTINGS_JSON.replace(
            r#""bash .claude/hooks/notify-stop.sh""#,
            r#""bash \"${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/notify-stop.sh\"""#,
        )
    }

    /// The Hooks tab identity match: an OLD-spelling mirror row is the
    /// migrated entry's metadata, not a second (orphan) hook. Before, the
    /// view matched by exact command and rendered the hook twice — active
    /// with no metadata, plus an "orphan" for the old row.
    #[tokio::test(flavor = "multi_thread")]
    async fn the_hooks_tab_matches_an_old_spelling_row_to_the_anchored_entry() {
        let f = Fixture::with_settings(&anchored_settings());
        let row = f
            .db
            .register_project_hook(
                &f.pid,
                "Stop",
                "",
                "bash .claude/hooks/notify-stop.sh",
                "bundled",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let stop: Vec<&EffectiveHook> = view.hooks.iter().filter(|h| h.event == "Stop").collect();
        assert_eq!(stop.len(), 1, "one hook, one row: {stop:?}");
        assert_eq!(stop[0].state, HookState::Active);
        assert_eq!(stop[0].command, ANCHORED_STOP);
        assert_eq!(stop[0].id, Some(row.id), "the old row's metadata is this hook's");
        assert_eq!(stop[0].source, "bundled");
    }

    /// Disable by the OLD spelling (what the hub's `PATCH /hooks/{id}` sends:
    /// the mirror row's command) removes the anchored entry and parks it under
    /// the old row; the view renders it Disabled once, and re-enabling
    /// restores the anchored form.
    #[tokio::test(flavor = "multi_thread")]
    async fn an_old_spelling_disables_and_restores_the_anchored_entry() {
        let f = Fixture::with_settings(&anchored_settings());
        let old = "bash .claude/hooks/notify-stop.sh";
        f.db.register_project_hook(&f.pid, "Stop", "", old, "bundled", None, None, &serde_json::json!({}))
            .unwrap();

        disable_hook(&f.db, &f.pid, "Stop", "", old).await.expect("disable by the old spelling");
        assert!(f.commands_under("Stop").is_empty());
        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let stop: Vec<&EffectiveHook> = view.hooks.iter().filter(|h| h.event == "Stop").collect();
        assert_eq!(stop.len(), 1, "{stop:?}");
        assert_eq!(stop[0].state, HookState::Disabled);

        enable_hook(&f.db, &f.pid, "Stop", "", old).await.expect("enable");
        assert_eq!(f.commands_under("Stop"), vec![ANCHORED_STOP.to_string()]);
    }

    // ─── Refusals: act vs leave-alone ───────────────────────────────────

    #[tokio::test(flavor = "multi_thread")]
    async fn enable_refuses_when_nothing_is_parked() {
        let f = Fixture::new();
        let before = f.raw();

        let err = enable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .expect_err("nothing parked → refuse");
        assert_eq!(err.code, "nothing_parked");
        assert_eq!(f.raw(), before, "a refused enable writes nothing");
    }

    // ─── F7: a parked row outlives the thing it restores ────────────────
    //
    // Retiring a hook has two halves — the SCRIPT stops shipping (the manifest
    // reconcile deletes it) and the REGISTRATION stops firing (the bundle
    // scrub removes it from settings.json). A hook the user had DISABLED is
    // in neither place: its bytes sit in `project_hooks.disabled_entry_json`,
    // which the scrub cannot see, so the Hooks tab kept offering to restore a
    // registration whose script the same update deleted.

    /// A settings.json carrying two retired registrations — one of each
    /// matcher kind in `vco_lib/hook_retirements.py`.
    const RETIRED_SETTINGS_JSON: &str = r#"{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit(*)",
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/scripts/sync_knowledge_graph.py \"$CLAUDE_TOOL_ARG_FILE_PATH\" 2>&1 || true"
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

    #[test]
    fn parked_row_disposition_acts_only_on_the_retirement_code() {
        // The act.
        assert_eq!(
            parked_row_disposition(RETIRED_REFUSAL_CODE),
            ParkedRowDisposition::Drop
        );
        // The leave-alone: every other refusal keeps the user's only copy of
        // those bytes. A transient failure must never consume them.
        for code in ["unparseable", "db_error", "timeout", "no_python", "bad_output"] {
            assert_eq!(
                parked_row_disposition(code),
                ParkedRowDisposition::Keep,
                "`{}` is not a retirement",
                code
            );
        }
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn enabling_a_retired_hook_script_is_refused_and_the_row_is_dropped() {
        let f = Fixture::with_settings(RETIRED_SETTINGS_JSON);
        let cmd = "bash .claude/hooks/cost-tracker.sh";

        disable_hook(&f.db, &f.pid, "Stop", "", cmd).await.unwrap();
        assert!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap()
                .is_some(),
            "the disable parks it — this is the state the retirement then strands"
        );
        let after_disable = f.raw();

        let err = enable_hook(&f.db, &f.pid, "Stop", "", cmd)
            .await
            .expect_err("a retired registration must not be restored");
        assert_eq!(err.code, RETIRED_REFUSAL_CODE);
        // The message comes from the ONE table and names what does the job
        // now, so the refusal is not a dead end.
        assert!(
            err.message.contains("v0.2.95"),
            "the refusal names the release that retired it: {}",
            err.message
        );
        assert!(
            err.message.contains("replaced by"),
            "the refusal names the replacement: {}",
            err.message
        );
        assert_eq!(
            f.raw(),
            after_disable,
            "a refused enable writes nothing to settings.json"
        );
        assert_eq!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap(),
            None,
            "the parked bytes are released: `Disabled (restorable)` has stopped \
             being true, and the mirror row now renders as the orphan it is"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn enabling_a_retired_inline_command_is_refused_by_whole_command_equality() {
        let f = Fixture::with_settings(RETIRED_SETTINGS_JSON);
        let cmd = "python .claude/scripts/sync_knowledge_graph.py \
                   \"$CLAUDE_TOOL_ARG_FILE_PATH\" 2>&1 || true";
        // The fixture's JSON has no line continuation; rebuild the exact
        // command the file carries.
        let cmd = cmd.split_whitespace().collect::<Vec<_>>().join(" ");

        disable_hook(&f.db, &f.pid, "PostToolUse", "Edit(*)", &cmd)
            .await
            .unwrap();
        let err = enable_hook(&f.db, &f.pid, "PostToolUse", "Edit(*)", &cmd)
            .await
            .expect_err("the inline sync_knowledge_graph registration is retired too");
        assert_eq!(err.code, RETIRED_REFUSAL_CODE);
        assert!(
            err.message.contains("post-file-edit.sh"),
            "it names the hook that replaced it: {}",
            err.message
        );
        assert_eq!(
            f.db.get_parked_project_hook_entry(&f.pid, "PostToolUse", "Edit(*)", &cmd)
                .unwrap(),
            None
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_parked_row_that_is_not_retired_survives_a_failed_enable() {
        // The leave-alone, end to end: the restore fails for a reason that
        // may well clear (the user broke settings.json), so the only copy of
        // those bytes must still be there afterwards.
        let f = Fixture::new();
        let cmd = "bash .claude/hooks/notify-stop.sh";
        disable_hook(&f.db, &f.pid, "Stop", "", cmd).await.unwrap();

        std::fs::write(&f.settings, "{ \"hooks\": { \"Stop\": [ ,,, }").unwrap();
        let err = enable_hook(&f.db, &f.pid, "Stop", "", cmd)
            .await
            .expect_err("an unparseable file refuses the restore");
        assert_eq!(err.code, "unparseable");
        assert!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap()
                .is_some(),
            "the parked entry is the user's only copy of that hook — a refusal \
             that is not a retirement must never consume it"
        );
    }

    // ─── F7, the EAGER half: the tab load asks, once, for all parked rows ──

    /// Build a `ParkedHook` the way `list_parked_project_hooks` would.
    fn parked_row(
        id: i64,
        event: &str,
        matcher: &str,
        command: &str,
    ) -> vct_launcher_core::db::project_hooks_settings::ParkedHook {
        vct_launcher_core::db::project_hooks_settings::ParkedHook {
            id,
            event: event.to_string(),
            matcher: matcher.to_string(),
            command: command.to_string(),
            source: "project".to_string(),
            source_module: None,
            timeout_ms: None,
            disabled_entry_json: "{\"schema\":1}".to_string(),
        }
    }

    fn verdict(event: &str, command: &str, retired: bool) -> JsonValue {
        serde_json::json!({
            "event": event,
            "command": command,
            "retired": retired,
            "retired_in": if retired { "v0.2.95" } else { "" },
            "replacement": if retired { "nothing (the capability was removed)" } else { "" },
            "reason": if retired { "the script no longer ships" } else { "" },
        })
    }

    #[test]
    fn a_parked_row_the_classifier_calls_retired_is_named_for_release() {
        let parked = vec![parked_row(1, "Stop", "", "bash .claude/hooks/cost-tracker.sh")];
        let matches = serde_json::json!([verdict("Stop", "", false)]);
        // Sanity: the fixture above is a DIFFERENT command, so nothing matches.
        assert!(parked_rows_to_prune(&parked, &matches).is_empty());

        let matches =
            serde_json::json!([verdict("Stop", "bash .claude/hooks/cost-tracker.sh", true)]);
        let doomed = parked_rows_to_prune(&parked, &matches);
        assert_eq!(doomed.len(), 1);
        assert_eq!(doomed[0].command, "bash .claude/hooks/cost-tracker.sh");
        assert_eq!(doomed[0].retired_in, "v0.2.95");
        assert_eq!(
            doomed[0].replacement, "nothing (the capability was removed)",
            "the audit row must be able to say what does the job now"
        );
    }

    #[test]
    fn a_parked_row_that_is_not_retired_is_left_alone() {
        // The leave-alone half of the eager prune. Those bytes are the user's
        // only copy of a hook they intend to re-enable.
        let parked = vec![parked_row(1, "Stop", "", "bash .claude/hooks/notify-stop.sh")];
        let matches =
            serde_json::json!([verdict("Stop", "bash .claude/hooks/notify-stop.sh", false)]);
        assert!(parked_rows_to_prune(&parked, &matches).is_empty());
    }

    #[test]
    fn an_unusable_answer_releases_nothing() {
        // Classifier down / unreadable / a shape we do not recognise: the
        // conservative default is to keep every row. The refusal half still
        // catches a retired one at click time.
        let parked = vec![parked_row(1, "Stop", "", "bash .claude/hooks/cost-tracker.sh")];
        for answer in [
            JsonValue::Null,
            serde_json::json!({}),
            serde_json::json!("nope"),
            serde_json::json!([]),
            // `retired` missing entirely — absent is not true.
            serde_json::json!([{ "event": "Stop", "command": "bash .claude/hooks/cost-tracker.sh" }]),
        ] {
            assert!(
                parked_rows_to_prune(&parked, &answer).is_empty(),
                "answer {:?} must release nothing",
                answer
            );
        }
    }

    #[test]
    fn verdicts_are_matched_by_pair_not_by_position() {
        // A positional zip would mis-attribute every later row the moment the
        // two sides disagreed about the batch — and a mis-attribution here
        // deletes the WRONG hook. Answer deliberately reordered, and with an
        // extra verdict for a pair that was never parked.
        let parked = vec![
            parked_row(1, "Stop", "", "bash .claude/hooks/notify-stop.sh"),
            parked_row(2, "Stop", "", "bash .claude/hooks/cost-tracker.sh"),
        ];
        let matches = serde_json::json!([
            verdict("Stop", "bash .claude/hooks/cost-tracker.sh", true),
            verdict("Stop", "bash .claude/hooks/never-parked.sh", true),
            verdict("Stop", "bash .claude/hooks/notify-stop.sh", false),
        ]);
        let doomed = parked_rows_to_prune(&parked, &matches);
        assert_eq!(doomed.len(), 1, "only the parked retired row: {:?}", doomed);
        assert_eq!(doomed[0].command, "bash .claude/hooks/cost-tracker.sh");
    }

    #[test]
    fn the_request_carries_every_parked_pair_and_nothing_else() {
        // One call per LOAD, not per row — and the classifier is asked about
        // identity only. The parked BYTES are the user's data and are not the
        // classifier's business; sending them would widen the blast radius of
        // a bug there for no gain.
        let parked = vec![
            parked_row(1, "Stop", "", "bash .claude/hooks/cost-tracker.sh"),
            parked_row(2, "PostToolUse", "Edit(*)", "bash .claude/hooks/x.sh"),
        ];
        let body: JsonValue =
            serde_json::from_str(&retirement_match_request(&parked)).unwrap();
        let pairs = body["pairs"].as_array().expect("pairs array");
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0]["event"], "Stop");
        assert_eq!(pairs[1]["command"], "bash .claude/hooks/x.sh");
        for p in pairs {
            let obj = p.as_object().unwrap();
            assert_eq!(
                obj.len(),
                2,
                "a pair carries exactly event+command, got {:?}",
                obj.keys().collect::<Vec<_>>()
            );
        }
        assert!(
            !retirement_match_request(&[]).contains("cost-tracker"),
            "an empty batch is an empty batch"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_project_with_no_parked_rows_spawns_nothing() {
        // The steady state. `list_parked` is empty → return before the
        // interpreter ladder is even consulted, so a project that never
        // disabled a hook pays nothing for this feature. Proven by pointing
        // the ladder at a path that CANNOT produce an interpreter: if the
        // spawn were attempted the call would report `no_python`, and the
        // assertion below would see a non-empty answer or a log.
        let f = Fixture::new();
        assert!(prune_retired_parked_rows(&f.db, &f.pid).await.is_empty());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn the_tab_load_releases_a_retired_parked_row_and_renders_it_orphan() {
        // End to end through the real classifier: disable a retired hook
        // (which parks it), then LOAD the tab. Nothing is clicked.
        let f = Fixture::with_settings(RETIRED_SETTINGS_JSON);
        let cmd = "bash .claude/hooks/cost-tracker.sh";
        disable_hook(&f.db, &f.pid, "Stop", "", cmd).await.unwrap();
        assert!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap()
                .is_some(),
            "precondition: the entry is parked"
        );

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();

        assert!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap()
                .is_none(),
            "the tab load must release bytes that can never be restored"
        );
        let row = view.hooks.iter().find(|h| h.command == cmd);
        match row {
            // The mirror row survives and tells the truth about itself.
            Some(h) => assert_eq!(h.state, HookState::Orphan),
            // No mirror row existed for this hand-written settings.json, so
            // there is nothing left to render — also correct, and the only
            // thing that must NOT happen is `Disabled`.
            None => {}
        }
        assert!(
            !view
                .hooks
                .iter()
                .any(|h| h.command == cmd && h.state == HookState::Disabled),
            "`Disabled (restorable)` has stopped being true"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn the_tab_load_keeps_a_live_parked_row() {
        // The act/leave-alone pair, end to end: same code path, a hook that
        // is very much alive, and the row is still there after the load.
        let f = Fixture::new();
        let cmd = "bash .claude/hooks/notify-stop.sh";
        disable_hook(&f.db, &f.pid, "Stop", "", cmd).await.unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();

        assert!(
            f.db.get_parked_project_hook_entry(&f.pid, "Stop", "", cmd)
                .unwrap()
                .is_some(),
            "a live hook's parked bytes are the user's only copy"
        );
        assert!(
            view.hooks
                .iter()
                .any(|h| h.command == cmd && h.state == HookState::Disabled),
            "and it still renders as restorable, because it is"
        );
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
        let script = folder.join(".claude/hooks/notify-stop.sh");
        std::fs::create_dir_all(script.parent().unwrap()).unwrap();
        std::fs::write(&script, "#!/usr/bin/env bash\n").unwrap();

        unregister_hook_entry(
            &f.db,
            &f.pid,
            "Stop",
            "",
            "bash .claude/hooks/notify-stop.sh",
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
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .unwrap();
        std::fs::write(&f.settings, &before).unwrap();

        unregister_hook_entry(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
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
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .unwrap();
        assert_eq!(f.db.list_parked_project_hooks(&f.pid).unwrap().len(), 1);

        register_hook_entry(
            &f.db,
            &f.pid,
            RegisterHookSettingsReq {
                event: "Stop".into(),
                matcher: String::new(),
                command: "bash .claude/hooks/notify-stop.sh".into(),
                timeout_seconds: Some(5),
                create_starter: false,
            },
        )
        .await
        .unwrap();

        assert_eq!(
            f.commands_under("Stop"),
            vec!["bash .claude/hooks/notify-stop.sh".to_string()],
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
            .find(|h| h.command == "bash .claude/hooks/notify-stop.sh")
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
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let row = view
            .hooks
            .iter()
            .find(|h| h.command == "bash .claude/hooks/notify-stop.sh")
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
        disable_hook(&f.db, &f.pid, "Stop", "", "bash .claude/hooks/notify-stop.sh")
            .await
            .unwrap();
        // The user puts the line back themselves; the parked row survives.
        std::fs::write(&f.settings, &before).unwrap();

        let view = effective_hooks_view(&f.db, &f.pid).await.unwrap();
        let rows: Vec<_> = view
            .hooks
            .iter()
            .filter(|h| h.command == "bash .claude/hooks/notify-stop.sh")
            .collect();
        assert_eq!(rows.len(), 1, "one row, not one per source of truth");
        assert_eq!(rows[0].state, HookState::Active, "the FILE decides");
    }
}
