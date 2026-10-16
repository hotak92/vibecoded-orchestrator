// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Model-guidance refresh triggers + the GUI status transport (v0.2.103,
//! plan item 12).
//!
//! The rendered model guidance (the `{{MODEL_SELECTION_GRID}}` and the
//! provider-conditional CLAUDE.md sections) follows the vendor keys the
//! user actually holds. The REFRESH itself is Python
//! (`python -m vco_lib.model_guidance refresh`) — rung A of the
//! cross-language ladder: this module only fires it and transports its
//! status payload. There is deliberately no Rust mirror of the provider
//! logic (which keys exist, what changed, what re-renders).
//!
//! Two triggers call [`spawn_model_guidance_refresh`]: launcher STARTUP
//! (`lib.rs` setup) and every Secrets-panel key mutation
//! (`secrets_cmd.rs`). Both are fire-and-forget: the spawn is detached,
//! every failure is one `tracing::warn` line, and nothing here ever blocks
//! launcher startup or a secrets command. The Python side writes only when
//! the rendered output changes.

use std::ffi::OsString;
use std::path::Path;

use tauri::{command, State};
use vct_launcher_core::process::CommandExt as _;

use crate::db::Db;

/// `refresh --trigger` value for the launcher-boot run.
pub(crate) const REFRESH_TRIGGER_STARTUP: &str = "startup";
/// `refresh --trigger` value for a Secrets-panel key change.
pub(crate) const REFRESH_TRIGGER_SECRETS: &str = "secrets";

/// The status read budgets the same ceiling as the other vco_lib status
/// transports (`embedding_catalog`).
const STATUS_TIMEOUT_SECS: u64 = 120;

/// The argv of a refresh run, after the interpreter's own `-m`.
/// Extracted so the tests pin the command shape without spawning.
fn build_refresh_args(trigger: &str) -> Vec<OsString> {
    vec![
        "vco_lib.model_guidance".into(),
        "refresh".into(),
        "--json".into(),
        "--trigger".into(),
        trigger.into(),
    ]
}

/// The argv of a status read, after the interpreter's own `-m`.
fn build_status_args() -> Vec<OsString> {
    vec!["vco_lib.model_guidance".into(), "status".into(), "--json".into()]
}

/// Detached spawn of the refresh with an already-resolved interpreter and
/// install root. The child owns its own lifecycle (stdio nulled, never
/// awaited) — the same fire-and-forget shape as
/// `module_enabled::spawn_re_render_claude_md`.
fn spawn_refresh_with(python: &Path, install_root: &Path, trigger: &str) -> std::io::Result<()> {
    std::process::Command::new(python)
        .silent()
        .arg("-m")
        .args(build_refresh_args(trigger))
        .current_dir(install_root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|_| ())
}

/// Fire the refresh from the launcher's DB-resolved install root (both
/// triggers: launcher startup and the Secrets panel). Soft-fail throughout:
/// a skipped refresh is a log line, never an error surfaced to the user —
/// at the secrets trigger the keychain change has already committed anyway.
pub(crate) fn spawn_model_guidance_refresh(db: &Db, trigger: &str) {
    let root = match crate::commands::installer::resolve_install_root_sync(db) {
        Some(root) => root,
        None => {
            tracing::warn!(
                "[vct] model-guidance refresh ({}): no install root resolved; skipping",
                trigger
            );
            return;
        }
    };
    let python = match vct_launcher_core::python_resolve::resolve_python_for_vco_lib() {
        Some(p) => p,
        None => {
            tracing::warn!(
                "[vct] model-guidance refresh ({}): no vco_lib python resolved; skipping",
                trigger
            );
            return;
        }
    };
    if let Err(e) = spawn_refresh_with(&python, &root, trigger) {
        tracing::warn!(
            "[vct] model-guidance refresh ({}): spawn failed: {}; skipping",
            trigger, e
        );
    }
}

/// The GUI's read: which providers were detected, the last-known record,
/// and when the guidance was last refreshed. Computed by
/// `python -m vco_lib.model_guidance status` (the ONE resolver, rung A) —
/// this command only transports it, verbatim, like the embedding-model
/// status commands beside it.
#[command]
pub async fn get_model_guidance_status(db: State<'_, Db>) -> Result<serde_json::Value, String> {
    let install_root = crate::commands::installer::resolve_install_root_sync(&db);
    let stdout = crate::commands::embedding_catalog::run_vco_lib_cli(
        &build_status_args(),
        install_root,
        STATUS_TIMEOUT_SECS,
        "model-guidance status",
    )
    .await?;
    crate::commands::embedding_catalog::parse_last_json_line(&stdout, "model-guidance status")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn arg_strings(args: &[OsString]) -> Vec<String> {
        args.iter().map(|a| a.to_string_lossy().into_owned()).collect()
    }

    #[test]
    fn refresh_args_invoke_the_python_home() {
        let args = arg_strings(&build_refresh_args("secrets"));
        assert_eq!(
            args,
            vec!["vco_lib.model_guidance", "refresh", "--json", "--trigger", "secrets"]
        );
    }

    #[test]
    fn status_args_invoke_the_python_home() {
        let args = arg_strings(&build_status_args());
        assert_eq!(args, vec!["vco_lib.model_guidance", "status", "--json"]);
    }

    /// BEHAVIOURAL: the spawn really runs `<python> -m vco_lib.model_guidance
    /// refresh --json --trigger <t>` with the install root as cwd — proven
    /// with a recording fake interpreter, never by scanning this file.
    #[cfg(unix)]
    #[test]
    fn spawn_refresh_with_runs_the_python_home_in_the_install_root() {
        let dir = tempfile::tempdir().unwrap();
        let install_root = dir.path().join("install-root");
        std::fs::create_dir_all(&install_root).unwrap();
        let log = dir.path().join("argv.log");
        let fake_python = dir.path().join("fake-python");
        std::fs::write(
            &fake_python,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\npwd >> '{}'\n",
                log.display(),
                log.display()
            ),
        )
        .unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&fake_python).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&fake_python, perms).unwrap();
        }

        spawn_refresh_with(&fake_python, &install_root, REFRESH_TRIGGER_SECRETS)
            .expect("the recording interpreter spawns");

        // The spawn is detached; give the child a bounded moment to write
        // (6 argv lines + the pwd line once it has run to completion).
        let mut content = String::new();
        for _ in 0..100 {
            if let Ok(read) = std::fs::read_to_string(&log) {
                content = read;
                if content.lines().count() >= 7 {
                    break;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(
            content.contains("-m\nvco_lib.model_guidance\nrefresh\n--json\n--trigger\nsecrets"),
            "the child was not invoked as the Python home; argv log: {content:?}"
        );
        let cwd_line = content.lines().last().unwrap_or("");
        let cwd = Path::new(cwd_line)
            .canonicalize()
            .unwrap_or_else(|_| std::path::PathBuf::from(cwd_line));
        assert_eq!(
            cwd,
            install_root.canonicalize().unwrap(),
            "the refresh must run with the install root as cwd"
        );
    }
}
