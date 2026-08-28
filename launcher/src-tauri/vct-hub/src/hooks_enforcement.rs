// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Real hook enforcement for the hub's two toggle surfaces (v0.2.91 wave 5
//! residual close, follow-up to decision #27).
//!
//! ## What this closes
//!
//! Decision #27 replaced the launcher GUI's Hooks-tab placebo with a real
//! writer: `commands::project_hooks_settings::{enable_hook, disable_hook}`
//! (`launcher/src-tauri/src`, Tauri-only) edit `<project>/.claude/settings.json`
//! through the ONE Python CLI, `python -m vco_lib.hooks_settings`, and park
//! the removed entry (`project_hooks.disabled_entry_json`, migration 042) so
//! a later re-enable is exact. Two OTHER surfaces kept calling the pre-#27
//! `Db::set_project_hook_enabled` — a bare `UPDATE project_hooks SET
//! enabled = ?` that nothing downstream reads:
//!
//!   * `PATCH /api/v1/projects/{project_id}/hooks/{hook_id}` (`project_state_api`)
//!   * `PATCH /api/v1/cli/hooks/{hook_id}/enabled` (`cli_api`), reachable
//!     from the shipped `vco hooks enable/disable <id> --project <p>` CLI
//!     (`launcher/tools/vct-cli`)
//!
//! Both silently did nothing to what actually runs — the review evidence
//! `.claude/context/reviews/v0291-wave5-phase2-ux-completeness` P2-B2 that
//! motivated #27 covered the GUI only; these two HTTP surfaces were missed.
//! This module closes the gap for the hub the same way #27 closed it for
//! the GUI: it drives the SAME `vco_lib.hooks_settings` writer and the SAME
//! parked-entry columns (`vct_launcher_core::db::project_hooks_settings`,
//! already shared between the launcher and the hub).
//!
//! ## Why this is a second Rust CALLER, not a second WRITER
//!
//! `vct-hub` is a standalone binary crate — its `Cargo.toml` has no
//! dependency on the Tauri app crate, by design, so a lightweight `axum`
//! server does not pull in Tauri/webview deps. The launcher's orchestration
//! helpers (`enable_hook` / `disable_hook` / `run_hooks_cli` /
//! `resolve_install_root_sync`) live in `launcher/src-tauri/src/commands/`,
//! private to that crate, and are therefore unreachable from here. Moving
//! them into `vct-launcher-core` so both binaries share one copy is the
//! correct long-term fix, but is out of scope for the lane that added this
//! module (contractually `vct-hub/src/**` only). This module is the
//! documented, deliberate exception: the duplication below is confined to
//! ORCHESTRATION (resolve the hook's identity, spawn the CLI, then update
//! the parked-entry columns) — the thing that actually EDITS
//! `settings.json` remains the single `vco_lib.hooks_settings` writer,
//! invoked here with the exact same subcommands/arguments the launcher
//! uses. There is still only one writer; there are now two callers of it.
//! If `vco_lib/hooks_settings.py`'s stdout-JSON contract changes, this
//! module must change to match — same obligation any other consumer of
//! that module has.
//!
//! ## Where the pieces come from (nothing here is a new resolver)
//!
//! * Orchestrator clone root (subprocess `cwd`, so the `vco_lib` namespace
//!   package resolves): `vct_launcher_core::orchestrator_manifest::
//!   find_orchestrator_manifest()`, walking up from `current_exe()` to
//!   `vct-module.json`. Already used by this same binary in production
//!   (`modules_api.rs`'s `/projects/{id}/env` resolver) — not a new
//!   resolution strategy invented for hooks.
//! * Python interpreter: `vct_launcher_core::python_resolve::
//!   resolve_python_for_vco_lib()` (the shared RT-4 ladder; its final tier
//!   is a bare `python3`/`python.exe` PATH fallback, sufficient here since
//!   `vco_lib.hooks_settings` only imports stdlib + `vco_lib.atomic` /
//!   `vco_lib.symlink_handler`, no third-party deps).
//! * Parked-entry storage: `Db::{park_project_hook_entry,
//!   get_parked_project_hook_entry, unpark_project_hook_entry,
//!   list_project_hooks}` — all already in `vct-launcher-core`, unchanged.

use std::path::PathBuf;
use std::time::Duration;

use axum::http::StatusCode;
use serde_json::Value as JsonValue;
use tokio::time::timeout;

use vct_launcher_core::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// Ceiling for one `vco_lib.hooks_settings` call. Mirrors
/// `commands::project_hooks_settings::HOOKS_CLI_TIMEOUT_SECS` (launcher/src)
/// — a single small JSON read/modify/write; anything past this is a stuck
/// interpreter, and a hung HTTP request would be worse than a legible
/// timeout error.
const HOOKS_CLI_TIMEOUT_SECS: u64 = 30;

/// A refusal or failure this module can produce, already carrying the HTTP
/// status the route handlers should return.
#[derive(Debug)]
pub(crate) struct HookEnforceError {
    status: StatusCode,
    code: String,
    message: String,
}

impl HookEnforceError {
    pub(crate) fn into_response(self) -> axum::response::Response {
        crate::http_error::error_response(self.status, &self.code, self.message)
    }
}

fn refuse(status: StatusCode, code: &str, message: impl Into<String>) -> HookEnforceError {
    HookEnforceError { status, code: code.to_string(), message: message.into() }
}

/// Resolve `(event, matcher, command)` for `hook_id`, scoped to
/// `project_id`. A hook is only ever toggled in the context of the project
/// that owns it — scoping the lookup this way means a `hook_id` that
/// belongs to a DIFFERENT project 404s instead of enforcing against the
/// wrong project's `settings.json`.
fn find_hook(
    db: &Db,
    project_id: &str,
    hook_id: i64,
) -> Result<(String, String, String), HookEnforceError> {
    let rows = db
        .list_project_hooks(project_id)
        .map_err(|e| refuse(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e))?;
    rows.into_iter()
        .find(|h| h.id == hook_id)
        .map(|h| (h.event, h.matcher, h.command))
        .ok_or_else(|| {
            refuse(
                StatusCode::NOT_FOUND,
                "hook_not_found",
                format!("no hook {} registered for project {}", hook_id, project_id),
            )
        })
}

fn project_folder(db: &Db, project_id: &str) -> Result<PathBuf, HookEnforceError> {
    match db.get_project(project_id) {
        Ok(Some(p)) => Ok(PathBuf::from(p.folder_path)),
        Ok(None) => Err(refuse(
            StatusCode::NOT_FOUND,
            "project_not_found",
            format!("project {} not found", project_id),
        )),
        Err(e) => Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e)),
    }
}

fn orchestrator_root() -> Result<PathBuf, HookEnforceError> {
    vct_launcher_core::orchestrator_manifest::find_orchestrator_manifest()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .ok_or_else(|| {
            refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "orchestrator_root_not_found",
                "cannot locate the orchestrator clone (vct-module.json) from the \
                 running hub binary — hook enforcement needs it to invoke \
                 `python -m vco_lib.hooks_settings`.",
            )
        })
}

/// Invoke `python -m vco_lib.hooks_settings <args>` and parse its
/// single-JSON stdout object. Mirrors
/// `commands::project_hooks_settings::run_hooks_cli` (launcher/src) — see
/// the module doc for why this is a deliberate second caller of the one
/// writer, not a second writer.
async fn run_hooks_cli(
    project_folder: &std::path::Path,
    args: &[&str],
) -> Result<JsonValue, HookEnforceError> {
    let root = orchestrator_root()?;
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib().ok_or_else(
        || {
            refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "no_python",
                "no Python interpreter found for vco_lib — hook enforcement cannot run.",
            )
        },
    )?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m").arg("vco_lib.hooks_settings");
    for a in args {
        cmd.arg(a);
    }
    cmd.arg("--project-folder").arg(project_folder.as_os_str());
    cmd.current_dir(&root);
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    let output = match timeout(Duration::from_secs(HOOKS_CLI_TIMEOUT_SECS), cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            return Err(refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "spawn_failed",
                format!("cannot run the hooks editor ({}): {}", python.display(), e),
            ))
        }
        Err(_) => {
            return Err(refuse(
                StatusCode::GATEWAY_TIMEOUT,
                "timeout",
                format!("the hooks editor did not finish within {HOOKS_CLI_TIMEOUT_SECS}s"),
            ))
        }
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let parsed: JsonValue = serde_json::from_str(stdout.trim()).map_err(|e| {
        refuse(
            StatusCode::INTERNAL_SERVER_ERROR,
            "bad_output",
            format!(
                "the hooks editor produced unreadable output ({}). stdout: {} stderr: {}",
                e,
                stdout.trim(),
                stderr.trim()
            ),
        )
    })?;

    if parsed.get("ok").and_then(JsonValue::as_bool) == Some(true) {
        return Ok(parsed);
    }
    let code = parsed.get("code").and_then(JsonValue::as_str).unwrap_or("unknown").to_string();
    let message = parsed
        .get("error")
        .and_then(JsonValue::as_str)
        .unwrap_or("the hooks editor refused the operation")
        .to_string();
    Err(refuse(StatusCode::BAD_REQUEST, &code, message))
}

/// The single entry point both hub routes call. Toggles hook `hook_id`
/// (scoped to `project_id`) the same way the launcher's Hooks tab does:
/// disabling REMOVES the entry from `settings.json` and parks it; enabling
/// restores the parked entry verbatim. Refuses — never silently no-ops —
/// when there is nothing parked to restore, mirroring
/// `commands::project_hooks_settings::enable_hook`'s `nothing_parked`
/// contract.
pub(crate) async fn enforce_hook_toggle(
    db: &Db,
    project_id: &str,
    hook_id: i64,
    enabled: bool,
) -> Result<(), HookEnforceError> {
    let (event, matcher, command) = find_hook(db, project_id, hook_id)?;
    let folder = project_folder(db, project_id)?;

    if enabled {
        let parked = db
            .get_parked_project_hook_entry(project_id, &event, &matcher, &command)
            .map_err(|e| refuse(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e))?
            .ok_or_else(|| {
                refuse(
                    StatusCode::CONFLICT,
                    "nothing_parked",
                    format!(
                        "no parked entry for `{}` under {} — VCO did not remove this \
                         hook, so it has nothing to restore.",
                        command, event
                    ),
                )
            })?;
        run_hooks_cli(&folder, &["enable", "--entry-json", &parked]).await?;
        db.unpark_project_hook_entry(project_id, &event, &matcher, &command)
            .map_err(|e| refuse(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e))?;
    } else {
        let result = run_hooks_cli(
            &folder,
            &["disable", "--event", &event, "--matcher", &matcher, "--command", &command],
        )
        .await?;
        let parked_json = result.get("parked_json").and_then(JsonValue::as_str).ok_or_else(|| {
            refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "bad_output",
                "the hooks editor removed the entry but returned no parked entry",
            )
        })?;
        db.park_project_hook_entry(project_id, &event, &matcher, &command, parked_json)
            .map_err(|e| {
                refuse(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "db_error",
                    vct_launcher_core::db::project_hooks_settings::park_failure_message(
                        parked_json,
                        &e,
                    ),
                )
            })?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use vct_launcher_core::db::models::ProjectHost;

    const SETTINGS_JSON: &str = r#"{
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
        /// Seed a project + a mirrored `project_hooks` row for every hook
        /// declared in `SETTINGS_JSON`, plus a real `.claude/settings.json`
        /// on disk — the shape `enforce_hook_toggle` needs end to end
        /// (DB row to resolve identity from `hook_id`, file to actually
        /// edit).
        fn new() -> Self {
            let td = tempfile::TempDir::new().unwrap();
            let claude = td.path().join(".claude");
            std::fs::create_dir_all(&claude).unwrap();
            let settings = claude.join("settings.json");
            std::fs::write(&settings, SETTINGS_JSON).unwrap();

            let db = Db::open_in_memory().unwrap();
            let pid = uuid::Uuid::new_v4().to_string();
            db.insert_project(
                &pid,
                "HooksEnforcementFixture",
                &td.path().to_string_lossy(),
                ProjectHost::Base,
                "hooks-enforcement-fixture",
            )
            .unwrap();
            db.register_project_hook(
                &pid,
                "PostToolUse",
                "Edit(*)",
                "bash .claude/hooks/post-tool-security.sh",
                "project",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();
            db.register_project_hook(
                &pid,
                "Stop",
                "",
                "bash .claude/hooks/cost-tracker.sh",
                "project",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();
            Self { db, pid, _td: td, settings }
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

        fn hook_id(&self, command: &str) -> i64 {
            self.db
                .list_project_hooks(&self.pid)
                .unwrap()
                .into_iter()
                .find(|h| h.command == command)
                .expect("hook must be mirrored")
                .id
        }
    }

    // ─── The red-proof: disabling must edit settings.json, not just the DB ──
    //
    // Pre-fix (de2e530a), both hub routes called `Db::set_project_hook_enabled`
    // — a bare `UPDATE project_hooks SET enabled = ?` — so this assertion
    // ("the entry is gone from the file") could not hold no matter what
    // `enabled` said. This test only passes once the route drives the real
    // `vco_lib.hooks_settings` writer.

    #[tokio::test(flavor = "multi_thread")]
    async fn disabling_a_hook_removes_its_entry_from_settings_json() {
        let f = Fixture::new();
        let before = f.raw();
        let hook_id = f.hook_id("bash .claude/hooks/post-tool-security.sh");

        enforce_hook_toggle(&f.db, &f.pid, hook_id, false)
            .await
            .expect("disable must succeed");

        assert_ne!(f.raw(), before, "settings.json MUST change — this is the fix");
        assert_eq!(
            f.commands_under("PostToolUse"),
            vec!["bash .claude/hooks/post-file-edit.sh".to_string()],
            "only the disabled hook is gone; its sibling stays"
        );
        assert_eq!(f.json()["userCustomKey"], "keep me", "unrelated keys survive untouched");

        let parked = f
            .db
            .get_parked_project_hook_entry(
                &f.pid,
                "PostToolUse",
                "Edit(*)",
                "bash .claude/hooks/post-tool-security.sh",
            )
            .unwrap();
        assert!(parked.is_some(), "the removed entry must be parked for an exact re-enable");
    }

    /// The pre-v0.2.91-parity mechanism, run verbatim, changes nothing that
    /// matters — pins the reason `enforce_hook_toggle` exists at all, and is
    /// the literal shape of the red-proof: on `de2e530a` this is ALL either
    /// hub route did.
    #[tokio::test(flavor = "multi_thread")]
    async fn the_bare_db_flag_flip_is_provably_inert() {
        let f = Fixture::new();
        let before = f.raw();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");

        f.db.set_project_hook_enabled(hook_id, false).unwrap();

        assert!(
            !f.db.list_project_hooks(&f.pid).unwrap().iter().find(|h| h.id == hook_id).unwrap().enabled,
            "the mirror flag flipped"
        );
        assert_eq!(
            f.raw(),
            before,
            "…and settings.json is untouched, so Claude Code still runs the hook — the placebo"
        );
        assert_eq!(f.commands_under("Stop"), vec!["bash .claude/hooks/cost-tracker.sh".to_string()]);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn re_enabling_restores_the_file_byte_for_byte() {
        let f = Fixture::new();
        let before = f.raw();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");

        enforce_hook_toggle(&f.db, &f.pid, hook_id, false).await.unwrap();
        assert!(f.commands_under("Stop").is_empty());

        enforce_hook_toggle(&f.db, &f.pid, hook_id, true)
            .await
            .expect("enable must succeed");

        assert_eq!(f.raw(), before, "re-enable restores the exact original bytes");
        assert_eq!(
            f.db
                .get_parked_project_hook_entry(&f.pid, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
                .unwrap(),
            None,
            "the parked entry is cleared once it is back in the file"
        );
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn enable_refuses_when_nothing_is_parked() {
        let f = Fixture::new();
        let before = f.raw();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");

        let err = enforce_hook_toggle(&f.db, &f.pid, hook_id, true)
            .await
            .expect_err("nothing parked -> refuse");
        assert_eq!(err.code, "nothing_parked");
        assert_eq!(err.status, StatusCode::CONFLICT);
        assert_eq!(f.raw(), before, "a refused enable writes nothing");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_hook_id_from_another_project_is_refused_not_enforced() {
        // A real `project_hooks` table is ONE table shared by every project
        // (a global autoincrement `id`), so the hazard this test pins is:
        // a real hook_id that genuinely belongs to project A, looked up
        // under project B, must 404 rather than resolve (a per-project
        // scan naturally can't find a row whose `project_id` column
        // doesn't match — this test is the regression guard for that scan
        // ever becoming a global-by-id lookup instead).
        let f = Fixture::new();
        let before = f.raw();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");

        // Second project in the SAME db (not a second Db — a hook_id is
        // only ever unique within one shared table).
        let other_pid = uuid::Uuid::new_v4().to_string();
        let other_td = tempfile::TempDir::new().unwrap();
        f.db.insert_project(
            &other_pid,
            "OtherProject",
            &other_td.path().to_string_lossy(),
            ProjectHost::Base,
            "other-project",
        )
        .unwrap();

        let err = enforce_hook_toggle(&f.db, &other_pid, hook_id, false)
            .await
            .expect_err("f's hook_id looked up under a different project must not resolve");
        assert_eq!(err.code, "hook_not_found");
        assert_eq!(f.raw(), before, "the ORIGINAL project's file is untouched");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn unknown_hook_id_in_the_right_project_is_refused() {
        let f = Fixture::new();
        let before = f.raw();

        let err = enforce_hook_toggle(&f.db, &f.pid, 999_999, false)
            .await
            .expect_err("no such hook_id -> refuse");
        assert_eq!(err.code, "hook_not_found");
        assert_eq!(f.raw(), before);
    }

    // ─── Interpreter resolution: the ladder, never a bare PATH lookup ───
    //
    // House rule (v0.2.91 wave 5 residual close, user-directed): every VCO
    // install runs `vco_lib` through ITS OWN venv, resolved via the shared
    // RT-4 ladder (`resolve_python_for_vco_lib`) — never a bare `python3`
    // resolved by the OS from PATH. `run_hooks_cli` calls that ladder
    // function rather than hardcoding an interpreter name; this test proves
    // it by making the ladder resolve to a FAKE interpreter (via
    // `$VCT_VENV`, the ladder's tier-1 override) and asserting the spawned
    // subprocess's own argv[0] is that fake path. If `run_hooks_cli` ever
    // regressed to `Command::new("python3")` (a bare PATH lookup, ignoring
    // the ladder), `$VCT_VENV` would have no effect and this test would
    // fail — the marker file the fake interpreter writes would never
    // appear, or would show a bare "python3"/"python" instead of the
    // fake's absolute path.
    //
    // The fake interpreter execs the real `python3` after recording its own
    // invocation path, so the underlying `vco_lib.hooks_settings disable`
    // still actually runs and the test also verifies end-to-end success —
    // not just "something was spawned".
    #[cfg(unix)]
    #[tokio::test(flavor = "multi_thread")]
    #[serial_test::serial]
    async fn hooks_cli_spawns_the_ladders_resolved_interpreter_not_a_bare_path_lookup() {
        use std::os::unix::fs::PermissionsExt;

        let venv_dir = tempfile::TempDir::new().unwrap();
        let bin = venv_dir.path().join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let fake_python = bin.join("python");
        let marker = venv_dir.path().join("invoked.marker");

        // Records its own path (argv[0] as invoked), then execs a REAL
        // python3 off PATH so the wrapped `hooks_settings` call still
        // completes for real — this test is not mocking the writer, only
        // proving WHICH interpreter path was handed to `Command::new`.
        let script = format!(
            "#!/bin/sh\nprintf '%s' \"$0\" > {}\nexec python3 \"$@\"\n",
            shell_quote(&marker)
        );
        std::fs::write(&fake_python, script).unwrap();
        let mut perms = std::fs::metadata(&fake_python).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&fake_python, perms).unwrap();

        let prev_venv = std::env::var_os("VCT_VENV");
        std::env::set_var("VCT_VENV", venv_dir.path());

        let f = Fixture::new();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");
        let result = enforce_hook_toggle(&f.db, &f.pid, hook_id, false).await;

        match prev_venv {
            Some(p) => std::env::set_var("VCT_VENV", p),
            None => std::env::remove_var("VCT_VENV"),
        }

        result.expect("disable through the fake-venv-resolved interpreter must still succeed");
        assert_eq!(f.commands_under("Stop"), Vec::<String>::new(), "the real writer still ran");

        let recorded = std::fs::read_to_string(&marker)
            .expect("the fake interpreter must have been invoked — no marker means the spawn never reached it");
        assert_eq!(
            Path::new(&recorded),
            fake_python.as_path(),
            "the spawned argv[0] must be the $VCT_VENV-resolved interpreter path, not a bare \
             `python3`/`python` PATH lookup"
        );
    }

    /// Minimal POSIX shell single-quoting for a path embedded in a
    /// generated `#!/bin/sh` script literal — the test tempdir path can
    /// contain nothing a single-quoted string can't hold, but quoting is
    /// still correct practice for any embedded path.
    #[cfg(unix)]
    fn shell_quote(p: &std::path::Path) -> String {
        format!("'{}'", p.to_string_lossy().replace('\'', r"'\''"))
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn disable_of_an_unparked_hook_then_disable_again_refuses_not_found() {
        // Leave-alone case for `find_hook`: once a hook is disabled its
        // settings.json entry is gone, but the `project_hooks` mirror row
        // (the thing `find_hook` reads) still exists — a second disable
        // must still resolve the row and hit the Python writer's own
        // `not_found` refusal (the entry really is gone from the file now),
        // not silently succeed a second time.
        let f = Fixture::new();
        let hook_id = f.hook_id("bash .claude/hooks/cost-tracker.sh");

        enforce_hook_toggle(&f.db, &f.pid, hook_id, false).await.unwrap();
        let after_first = f.raw();

        let err = enforce_hook_toggle(&f.db, &f.pid, hook_id, false)
            .await
            .expect_err("already disabled -> the writer refuses, not a silent no-op");
        assert_eq!(err.code, "not_found");
        assert_eq!(f.raw(), after_first, "the second, refused disable writes nothing further");
    }
}
