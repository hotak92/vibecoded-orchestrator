// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//!
//! The machine-wide per-provider switch (v0.2.105) — the Home page's
//! "Model providers" card.
//!
//! ## What it switches, and where the choice lives
//!
//! Every vendor the model gateway knows (`claude-gw/<vendor>/…`) can be turned
//! OFF once, machine-wide. The change takes full effect AT ONCE (v0.2.106):
//! the helper's stored `set` itself parks (off) or restores (on) the vendor's
//! gateway agent definitions in the orchestrator root and every registered
//! project (through the bundle engine's own park/restore functions — a
//! previously delivered, unedited copy is moved to `.claude/agents.disabled/`,
//! never deleted), the guidance refresh re-renders every CLAUDE.md's model
//! rows (fired from here, see "After a write"), and the RUNNING gateway stops
//! serving the vendor within its short cache TTL — its rows leave
//! `/v1/models`, requests naming its ids are refused locally, the quota
//! fallback ladder skips it, and `/vendor-keys` reports it so the subagent
//! pre-flight refuses its launches.
//!
//! The choice is ONE `launcher.db` `app_state` row (`model_providers.disabled`,
//! a JSON array of the OFF vendor ids) whose reader AND writer are
//! `vco_lib.model_providers` — rung A of the cross-language ladder. This module
//! spawns that helper (`python -m vco_lib.model_providers status|set --json`)
//! and passes its payload through: no Rust mirror of the encoding, the vendor
//! list, or the key state. Two reasons, both structural: the setting is
//! consumed by Python (the delivery gate and the guidance render), so a second
//! implementation of the row's shape is a copy nothing compile-checks; and the
//! vendor list must stay "a row in `model_router.vendors`, never a code
//! change".
//!
//! ## Names only
//!
//! `status` reports each vendor's KEY STATE (`present` / `absent` /
//! `unavailable` / `unknown`) and its declared secret-key NAMES. A key VALUE
//! never leaves the gateway's resolver, and nothing here could carry one: the
//! payload is parsed and returned verbatim, never inspected for values.
//!
//! ## After a write
//!
//! A stored change fires the model-guidance refresh
//! ([`crate::commands::model_guidance::spawn_model_guidance_refresh`]) with the
//! `providers` trigger, so the CLAUDE.md files of every registered project
//! follow the switch now rather than at the next release — the same
//! fire-and-forget contract as the Secrets-panel trigger. A write that could
//! NOT be stored answers `stored: false` (the helper's own verdict) and fires
//! nothing: a switch that does not stick must not look applied.

use std::time::Duration;

use tauri::{command, State};

use crate::db::Db;

use super::model_gateway::{python_module_command, python_or_err, run_to_completion_within};

/// The helper's own ceiling: one DB read plus one key-state probe per vendor.
const PROVIDER_TIMEOUT: Duration = Duration::from_secs(120);

/// The argv tail of a `status` read, after the interpreter's own `-m`.
/// Split out so the argv shape is unit-testable without a Python.
fn status_args() -> Vec<String> {
    vec!["status".to_string(), "--json".to_string()]
}

/// The argv tail of a `set` write. The vendor id goes on the argv VERBATIM
/// (the helper validates it against the registry and refuses an unknown one
/// with exit 2 — a typo must not silently create a setting nothing reads).
fn set_args(vendor: &str, enabled: bool) -> Vec<String> {
    vec![
        "set".to_string(),
        "--vendor".to_string(),
        vendor.to_string(),
        if enabled { "--enabled".to_string() } else { "--disabled".to_string() },
        "--json".to_string(),
    ]
}

/// Spawn `python -m vco_lib.model_providers <args>` with the launcher's env
/// sandbox and the clone's `PYTHONPATH` (the same recipe as the gateway
/// agents gate beside it), and return its parsed JSON payload.
///
/// A non-zero exit whose stdout still parses is a LEGITIMATE structured
/// answer (`set` answers `stored: false` when the launcher DB could not be
/// written) and is returned as-is; the GUI renders it. Only an unparseable
/// answer is an error.
fn run_model_providers(args: Vec<String>) -> Result<serde_json::Value, String> {
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    run_model_providers_with(&python, root.as_deref(), args)
}

/// [`run_model_providers`] with an ALREADY-RESOLVED interpreter and install
/// root, so the argv and the payload round-trip are provable with a recording
/// fake interpreter (no real Python, no env mutation).
fn run_model_providers_with(
    python: &std::path::Path,
    orchestrator_root: Option<&std::path::Path>,
    args: Vec<String>,
) -> Result<serde_json::Value, String> {
    let mut cmd = python_module_command(python, "vco_lib.model_providers", orchestrator_root);
    for a in args {
        cmd.arg(a);
    }
    let (code, stdout, stderr) =
        run_to_completion_within(cmd, "vco_lib.model_providers", PROVIDER_TIMEOUT)?;
    let body = stdout.trim();
    if body.is_empty() {
        return Err(format!(
            "vco_lib.model_providers exited {} and printed nothing: {}",
            code,
            stderr.trim()
        ));
    }
    // The helper prints exactly one JSON object on stdout; its human notes go
    // to stderr. `whole` is the strict case, `last line` tolerates a stray
    // warning line ahead of the payload.
    serde_json::from_str::<serde_json::Value>(body).or_else(|e| {
        let last = body.lines().last().unwrap_or("");
        serde_json::from_str::<serde_json::Value>(last).map_err(|_| {
            format!(
                "vco_lib.model_providers exited {} and did not return JSON ({}): {}",
                code,
                e,
                stderr.trim()
            )
        })
    })
}

/// Every gateway provider with its on/off state, its key state (a NAME-level
/// answer) and the agent definitions it owns. Reads only.
#[command]
pub async fn model_providers_status() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(|| run_model_providers(status_args()))
        .await
        .map_err(|e| format!("model-providers status task failed: {}", e))?
}

/// Switch ONE provider on or off machine-wide. The answer carries
/// `stored` — `false` means the launcher database could not be written, so
/// the GUI must say the change was not saved instead of showing it applied.
///
/// A stored change fires the guidance refresh (see the module docstring).
#[command]
pub async fn model_providers_set(
    db: State<'_, Db>,
    vendor: String,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    let payload = tauri::async_runtime::spawn_blocking(move || {
        run_model_providers(set_args(&vendor, enabled))
    })
    .await
    .map_err(|e| format!("model-providers set task failed: {}", e))??;
    if payload.get("stored").and_then(|v| v.as_bool()).unwrap_or(false) {
        // Fire-and-forget, exactly like the Secrets-panel trigger: the write
        // has already committed, so a skipped refresh is a log line, never an
        // error the user must act on.
        crate::commands::model_guidance::spawn_model_guidance_refresh(
            &db,
            crate::commands::model_guidance::REFRESH_TRIGGER_PROVIDERS,
        );
    }
    Ok(payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_args_invoke_the_python_home() {
        assert_eq!(status_args(), vec!["status", "--json"]);
    }

    #[test]
    fn set_args_carry_the_vendor_verbatim_and_one_direction() {
        assert_eq!(
            set_args("kimi", false),
            vec!["set", "--vendor", "kimi", "--disabled", "--json"]
        );
        assert_eq!(
            set_args("qwen", true),
            vec!["set", "--vendor", "qwen", "--enabled", "--json"]
        );
    }

    /// A vendor id the registry does not know is refused by the HELPER (exit
    /// 2, no row written) — the argv is never the place a typo is caught, so
    /// the id must travel unaltered and unmapped.
    #[test]
    fn set_args_never_rewrite_the_vendor_id() {
        let args = set_args("Kimi ", false);
        assert_eq!(args[2], "Kimi ");
    }

    /// BEHAVIOURAL: the spawn really runs
    /// `<python> -m vco_lib.model_providers <args>` and hands the child's JSON
    /// back verbatim — proven with a recording fake interpreter, never by
    /// scanning this file.
    #[cfg(unix)]
    #[test]
    fn run_model_providers_with_runs_the_python_home_and_returns_its_json() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("argv.log");
        let fake_python = dir.path().join("fake-python");
        std::fs::write(
            &fake_python,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\n\
                 printf '{{\"vendor\":\"kimi\",\"enabled\":false,\"stored\":true}}\\n'\n",
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

        let payload = run_model_providers_with(
            &fake_python,
            None,
            set_args("kimi", false),
        )
        .expect("the recording interpreter answers JSON");

        assert_eq!(payload["vendor"], "kimi");
        assert_eq!(payload["stored"], true);
        let argv = std::fs::read_to_string(&log).unwrap();
        assert!(
            argv.contains("-m\nvco_lib.model_providers\nset\n--vendor\nkimi\n--disabled\n--json"),
            "the child was not invoked as the Python home; argv log: {argv:?}"
        );
    }
}