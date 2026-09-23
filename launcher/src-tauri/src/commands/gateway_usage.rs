// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Subscription usage windows for the home-page card — the launcher half.
//!
//! The gateway computes and caches the windows (`model_router.usage_windows`,
//! `GET /usage/windows`); `python -m vco_lib.gateway_usage` reads them with
//! the host token. This command only spawns that bridge and relays its one
//! JSON object, for the reason every gateway call from the launcher goes
//! through Python (A>B>C, rule A): the host token never crosses this process
//! (see `model_gateway.rs`, "The host token never crosses this process").
//!
//! The card polls, so the not-running case is answered HERE from the pid
//! file without spawning an interpreter — the same short-circuit
//! `gateway_freshness` takes.

use tauri::command;

use crate::commands::model_gateway::{
    gateway_usage_command, probe_process, python_or_err, run_to_completion_within, ProcessState,
};

/// The bridge makes one loopback GET answered from the gateway's memory
/// (its own timeout is 3 s); the rest is interpreter start-up.
const BRIDGE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);

/// The answer for a gateway that is not running. Same shape as the bridge's
/// own failures (`vco_lib.gateway_usage._failure`), so the card has one
/// branch for "no numbers".
pub fn not_running_payload() -> serde_json::Value {
    serde_json::json!({
        "ok": false,
        "reason": "not_running",
        "message": "the model gateway is not running",
    })
}

/// Parse the bridge's stdout. Its stdout is a machine contract — one JSON
/// object, exit 0 even for "unreachable" — so anything else is a broken
/// install and is reported as an error carrying the bridge's stderr.
pub fn parse_bridge_output(
    code: i32,
    stdout: &str,
    stderr: &str,
) -> Result<serde_json::Value, String> {
    match serde_json::from_str::<serde_json::Value>(stdout.trim()) {
        Ok(v) if v.get("ok").and_then(|ok| ok.as_bool()).is_some() => Ok(v),
        Ok(_) => Err(format!(
            "vco_lib.gateway_usage exited {} with JSON that is not a usage answer: {}",
            code,
            stderr.trim()
        )),
        Err(e) => Err(format!(
            "vco_lib.gateway_usage exited {} and did not return JSON ({}): {}",
            code,
            e,
            stderr.trim()
        )),
    }
}

fn fetch_blocking() -> Result<serde_json::Value, String> {
    if probe_process().0 != ProcessState::Running {
        return Ok(not_running_payload());
    }
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let cmd = gateway_usage_command(&python, root.as_deref());
    let (code, stdout, stderr) =
        run_to_completion_within(cmd, "vco_lib.gateway_usage", BRIDGE_TIMEOUT)?;
    parse_bridge_output(code, &stdout, &stderr)
}

/// `{"ok": true, "port": N, "snapshot": {...}}` or
/// `{"ok": false, "reason": ..., "message": ...}`.
#[command]
pub async fn model_gateway_usage_windows() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(fetch_blocking)
        .await
        .map_err(|e| format!("usage-windows task failed: {}", e))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_bridge_success_and_failure_shapes_both_parse() {
        let ok = parse_bridge_output(
            0,
            r#"{"ok": true, "port": 11460, "snapshot": {"vendors": []}}"#,
            "",
        )
        .unwrap();
        assert_eq!(ok["port"], 11460);
        let failed = parse_bridge_output(
            0,
            r#"{"ok": false, "reason": "unreachable", "message": "m"}"#,
            "",
        )
        .unwrap();
        assert_eq!(failed["reason"], "unreachable");
    }

    /// F4: a broken install exits 1 WITH its JSON answer. It must reach the
    /// card as that answer (so the card can name the problem), not be
    /// discarded for the non-zero exit.
    #[test]
    fn a_broken_install_answer_survives_its_nonzero_exit() {
        let v = parse_bridge_output(
            1,
            r#"{"ok": false, "reason": "broken_install", "message": "re-run install.py"}"#,
            "vco_lib.gateway_usage: broken install",
        )
        .unwrap();
        assert_eq!(v["reason"], "broken_install");
    }

    #[test]
    fn anything_else_is_an_error_carrying_stderr() {
        let err = parse_bridge_output(
            1,
            "Traceback (most recent call last)",
            "ImportError: vco_lib",
        )
        .unwrap_err();
        assert!(err.contains("ImportError: vco_lib"), "{err}");
        let err = parse_bridge_output(0, r#"{"vendors": []}"#, "").unwrap_err();
        assert!(err.contains("not a usage answer"), "{err}");
    }

    #[test]
    fn not_running_has_the_bridge_failure_shape() {
        let v = not_running_payload();
        assert_eq!(v["ok"], false);
        assert_eq!(v["reason"], "not_running");
        assert!(v["message"].as_str().is_some_and(|m| !m.is_empty()));
    }
}
