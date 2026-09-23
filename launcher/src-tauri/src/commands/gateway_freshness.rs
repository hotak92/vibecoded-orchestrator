// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Post-update "the model gateway needs restarting" — the launcher half.
//!
//! An orchestrator update rewrites the gateway's editable install underneath
//! a running daemon and restarts nothing, so the gateway can keep serving the
//! previous release's code for as long as it stays up. It is NEVER restarted
//! automatically: the VS Code panel routes every chat through it, a restart
//! kills live agent sessions, and the owner's rule is absolute ("session
//! should not become unusable. full stop."). Instead the launcher shows a
//! modal after an update — Continue restarts it, Dismiss leaves it.
//!
//! ## Where each decision lives (A>B>C, rule A)
//!
//! Staleness and the init-system restart are decided ONCE, in Python:
//! `python -m vco_lib.gateway_freshness check|restart --json`. The daemon
//! reports a source digest on `/health`, the module hashes the checkout with
//! the same file, and only a positive mismatch prompts — "could not tell" is
//! silence. This file adds exactly one thing Python cannot know: whether THIS
//! launcher holds the gateway as a child process, because only the holder of
//! the child handle may stop it (the same stance `model_gateway_stop` takes —
//! a pid read from a file is never signalled).
//!
//! ## When the modal is offered
//!
//! Both update surfaces (the MenuBar badge and Preferences → Updates) end in a
//! launcher restart, so the frontend asks once per launcher start and again
//! after an in-process update; the check is cheap when nothing is running
//! (the pid-file probe short-circuits before any interpreter is spawned).

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::commands::single_flight::{self, SingleFlightGuard, OP_GATEWAY_RESTART};
use crate::commands::model_gateway::{
    gateway_freshness_command, probe_process, python_or_err, run_to_completion_within,
    GatewaySupervisor, ProcessState,
};

/// `check` is one `/health` GET plus, when stale, one `systemctl show`.
const CHECK_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);
/// `restart` waits for the old process to go and the new one to serve the
/// checkout's source (`VERIFY_WAIT_S` + `STOP_WAIT_S` in the Python module).
const RESTART_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(90);

/// The mechanism word this module adds on top of the Python plan.
pub const MECH_LAUNCHER: &str = "launcher";

/// How a stale gateway could be restarted. Mirrors
/// `vco_lib.gateway_freshness.RestartPlan.to_dict`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RestartPlan {
    #[serde(default)]
    pub mechanism: String,
    #[serde(default)]
    pub possible: bool,
    #[serde(default)]
    pub reason: String,
}

/// Mirrors `vco_lib.gateway_freshness.FreshnessReport.to_dict`. Every field is
/// defaulted: a report the launcher cannot fully parse must degrade to "no
/// prompt", never to a guess.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct FreshnessReport {
    #[serde(default)]
    pub verdict: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub running_version: Option<String>,
    #[serde(default)]
    pub checkout_version: Option<String>,
    #[serde(default)]
    pub served_sha: Option<String>,
    #[serde(default)]
    pub expected_sha: Option<String>,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub port: Option<u16>,
    /// The ONE field the modal branches on. True only for a PROVEN-stale
    /// gateway; see the Python module's verdict arms.
    #[serde(default)]
    pub prompt: bool,
    #[serde(default)]
    pub restart: Option<RestartPlan>,
}

impl FreshnessReport {
    fn not_running() -> Self {
        FreshnessReport {
            verdict: "not_running".to_string(),
            summary: "model gateway: not running.".to_string(),
            ..Default::default()
        }
    }
}

/// What Continue did. `outcome` uses the Python module's words
/// (`restarted` / `not_needed` / `unsupported` / `unverified`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestartResult {
    pub outcome: String,
    pub restarted: bool,
    pub message: String,
}

/// Which path Continue takes. Pure, so both destructive branches and every
/// leave-alone branch are unit-tested without a daemon.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestartRoute {
    /// Not proven stale — nothing is restarted.
    NotNeeded,
    /// Stale, and this launcher holds the gateway as its own child. `pid` is
    /// the child the decision was made ABOUT; only that child may be stopped
    /// (review R1 F9 — the supervisor can replace it during the check).
    LauncherChild { pid: u32 },
    /// Stale, not ours — the Python module restarts it through its init
    /// system, or refuses with a reason (it re-checks and re-plans itself).
    InitSystem,
}

pub fn decide_restart(report: &FreshnessReport, held_child_pid: Option<u32>) -> RestartRoute {
    if !report.prompt {
        return RestartRoute::NotNeeded;
    }
    match (held_child_pid, report.pid) {
        (Some(held), Some(serving)) if held == serving => RestartRoute::LauncherChild { pid: held },
        _ => RestartRoute::InitSystem,
    }
}

/// Put this launcher's own mechanism into a stale report when it holds the
/// serving process. Python cannot see the child handle, so without this a
/// launcher-started gateway would read "no service manager owns it".
pub fn overlay_launcher_child(mut report: FreshnessReport, held_child_pid: Option<u32>) -> FreshnessReport {
    if matches!(decide_restart(&report, held_child_pid), RestartRoute::LauncherChild { .. }) {
        report.restart = Some(RestartPlan {
            mechanism: MECH_LAUNCHER.to_string(),
            possible: true,
            reason: "this launcher started the gateway; it is stopped and started \
                     again on the same port"
                .to_string(),
        });
    }
    report
}

/// Outcome word for a Continue refused because a restart is already running.
pub const OUTCOME_IN_PROGRESS: &str = "in_progress";

/// Claim the one process-wide gateway restart, or the result to return
/// instead. A refusal restarts nothing and is not an error: the restart the
/// user asked for IS happening.
fn claim_restart() -> Result<SingleFlightGuard, RestartResult> {
    single_flight::try_begin(OP_GATEWAY_RESTART).ok_or_else(|| RestartResult {
        outcome: OUTCOME_IN_PROGRESS.to_string(),
        restarted: false,
        message: "A gateway restart is already running; not starting a second one."
            .to_string(),
    })
}

fn run_freshness(args: &[&str], timeout: std::time::Duration) -> Result<serde_json::Value, String> {
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_freshness_command(&python, root.as_deref());
    for a in args {
        cmd.arg(a);
    }
    if let Some(root) = root.as_deref() {
        cmd.arg("--install-root").arg(root);
    }
    let (code, stdout, stderr) =
        run_to_completion_within(cmd, "vco_lib.gateway_freshness", timeout)?;
    serde_json::from_str::<serde_json::Value>(stdout.trim()).map_err(|e| {
        format!(
            "vco_lib.gateway_freshness exited {} and did not return JSON ({}): {}",
            code,
            e,
            stderr.trim()
        )
    })
}

fn check_blocking() -> Result<FreshnessReport, String> {
    // No interpreter for the common case: nothing running, nothing stale.
    if probe_process().0 != ProcessState::Running {
        return Ok(FreshnessReport::not_running());
    }
    let value = run_freshness(&["check", "--json"], CHECK_TIMEOUT)?;
    serde_json::from_value::<FreshnessReport>(value)
        .map_err(|e| format!("unexpected gateway_freshness check payload: {}", e))
}

/// Is the running gateway behind the checkout? Starts nothing.
#[command]
pub async fn model_gateway_freshness(
    supervisor: State<'_, GatewaySupervisor>,
) -> Result<FreshnessReport, String> {
    let held = supervisor.held_child_pid();
    let report = tauri::async_runtime::spawn_blocking(check_blocking)
        .await
        .map_err(|e| format!("freshness check task failed: {}", e))??;
    Ok(overlay_launcher_child(report, held))
}

fn verify_after_launcher_restart() -> FreshnessReport {
    // The respawned child needs a moment to bind and hash; poll the same
    // check rather than trusting the spawn.
    // Each attempt spawns the Python check, so poll at 1 s for up to 30 s —
    // the same budget as the Python module's own VERIFY_WAIT_S.
    let mut last = FreshnessReport::not_running();
    for _ in 0..30 {
        std::thread::sleep(std::time::Duration::from_secs(1));
        match check_blocking() {
            Ok(r) if r.verdict == "current" => return r,
            Ok(r) => last = r,
            Err(_) => {}
        }
    }
    last
}

/// Continue: restart the gateway — only if it is PROVEN stale, and only
/// through something that owns it.
#[command]
pub async fn model_gateway_restart_stale(
    supervisor: State<'_, GatewaySupervisor>,
) -> Result<RestartResult, String> {
    // Held for the whole command — check, restart AND verify — so a second
    // Continue (another window, a double-fire, a re-armed button) cannot start
    // a second restart while the first is still replacing the process
    // (review R1 F3, backend half). Released by Drop on every return path.
    let _claim = match claim_restart() {
        Ok(guard) => guard,
        Err(refusal) => return Ok(refusal),
    };
    let held = supervisor.held_child_pid();
    let report = tauri::async_runtime::spawn_blocking(check_blocking)
        .await
        .map_err(|e| format!("freshness check task failed: {}", e))??;
    match decide_restart(&report, held) {
        RestartRoute::NotNeeded => Ok(RestartResult {
            outcome: "not_needed".to_string(),
            restarted: false,
            message: format!("Nothing restarted — {}", report.summary),
        }),
        RestartRoute::LauncherChild { pid: decided_pid } => {
            let restarted = supervisor.restart_held_child(decided_pid)?;
            let Some((pid, port)) = restarted else {
                return Ok(RestartResult {
                    outcome: "not_needed".to_string(),
                    restarted: false,
                    message: "The gateway this launcher started is no longer the process \
                              that was found stale (it exited or was already replaced); \
                              nothing restarted."
                        .to_string(),
                });
            };
            tracing::info!(
                "[vct] model gateway: restarted the launcher's child after an update \
                 (pid {}, port {})",
                pid,
                port
            );
            let after = tauri::async_runtime::spawn_blocking(verify_after_launcher_restart)
                .await
                .map_err(|e| format!("verify task failed: {}", e))?;
            Ok(if after.verdict == "current" {
                RestartResult {
                    outcome: "restarted".to_string(),
                    restarted: true,
                    message: "Model gateway restarted; it now serves the updated source."
                        .to_string(),
                }
            } else {
                RestartResult {
                    outcome: "unverified".to_string(),
                    restarted: false,
                    message: format!(
                        "The gateway was restarted on port {}, but does not yet serve the \
                         updated source ({}).",
                        port, after.summary
                    ),
                }
            })
        }
        RestartRoute::InitSystem => {
            let value = tauri::async_runtime::spawn_blocking(|| {
                run_freshness(&["restart", "--json"], RESTART_TIMEOUT)
            })
            .await
            .map_err(|e| format!("restart task failed: {}", e))??;
            serde_json::from_value::<RestartResult>(value)
                .map_err(|e| format!("unexpected gateway_freshness restart payload: {}", e))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stale(pid: Option<u32>) -> FreshnessReport {
        FreshnessReport {
            verdict: "stale".to_string(),
            prompt: true,
            pid,
            restart: Some(RestartPlan {
                mechanism: "none".to_string(),
                possible: false,
                reason: "not registered".to_string(),
            }),
            ..Default::default()
        }
    }

    #[test]
    fn nothing_is_restarted_when_not_proven_stale() {
        for verdict in ["current", "unknown", "not_running"] {
            let report = FreshnessReport {
                verdict: verdict.to_string(),
                prompt: false,
                pid: Some(42),
                ..Default::default()
            };
            // Even a held child with the same pid does not make it a restart.
            assert_eq!(decide_restart(&report, Some(42)), RestartRoute::NotNeeded, "{verdict}");
            assert_eq!(decide_restart(&report, None), RestartRoute::NotNeeded, "{verdict}");
        }
    }

    #[test]
    fn a_stale_gateway_this_launcher_holds_is_restarted_by_the_launcher() {
        // The route carries the pid the decision was about (review R1 F9), so
        // the stop can refuse a child the supervisor swapped in meanwhile.
        assert_eq!(
            decide_restart(&stale(Some(7)), Some(7)),
            RestartRoute::LauncherChild { pid: 7 }
        );
    }

    #[test]
    fn a_second_restart_while_one_is_in_flight_is_refused_without_acting() {
        let first = claim_restart().expect("the first Continue claims the restart");
        let second = claim_restart().expect_err("a concurrent Continue must be refused");
        assert_eq!(second.outcome, OUTCOME_IN_PROGRESS);
        assert!(!second.restarted);
        drop(first);
        // Sequential re-runs are allowed once the first has finished.
        let again = claim_restart().expect("released on drop");
        drop(again);
    }

    #[test]
    fn a_stale_gateway_someone_else_started_goes_to_the_init_system_path() {
        // Held child is a DIFFERENT process: never signal it for this one.
        assert_eq!(decide_restart(&stale(Some(7)), Some(8)), RestartRoute::InitSystem);
        assert_eq!(decide_restart(&stale(Some(7)), None), RestartRoute::InitSystem);
        assert_eq!(decide_restart(&stale(None), Some(7)), RestartRoute::InitSystem);
    }

    #[test]
    fn the_overlay_names_the_launcher_only_for_its_own_child() {
        let mine = overlay_launcher_child(stale(Some(7)), Some(7));
        let plan = mine.restart.expect("plan");
        assert_eq!(plan.mechanism, MECH_LAUNCHER);
        assert!(plan.possible);

        let theirs = overlay_launcher_child(stale(Some(7)), Some(8));
        assert_eq!(theirs.restart.expect("plan").mechanism, "none");
    }

    #[test]
    fn the_overlay_never_turns_a_current_gateway_into_a_prompt() {
        let current = FreshnessReport {
            verdict: "current".to_string(),
            prompt: false,
            pid: Some(7),
            ..Default::default()
        };
        let out = overlay_launcher_child(current.clone(), Some(7));
        assert_eq!(out, current);
    }

    #[test]
    fn the_python_payload_parses_and_an_incomplete_one_degrades_to_no_prompt() {
        let full = serde_json::json!({
            "verdict": "stale", "summary": "s", "running_version": "0.2.96",
            "checkout_version": "0.2.97", "served_sha": null, "expected_sha": "abc",
            "pid": 12, "port": 11460, "prompt": true,
            "restart": {"mechanism": "boot_service", "possible": true, "reason": "r"},
        });
        let parsed: FreshnessReport = serde_json::from_value(full).expect("parse");
        assert!(parsed.prompt);
        assert_eq!(parsed.port, Some(11460));
        assert_eq!(parsed.restart.expect("plan").mechanism, "boot_service");

        let partial: FreshnessReport =
            serde_json::from_value(serde_json::json!({"verdict": "stale"})).expect("parse");
        assert!(!partial.prompt, "a payload without `prompt` must not prompt");
    }

    #[test]
    fn the_restart_payload_parses() {
        let v = serde_json::json!({
            "outcome": "restarted", "restarted": true, "message": "m",
            "before": null, "after": null, "commands": [],
        });
        let r: RestartResult = serde_json::from_value(v).expect("parse");
        assert!(r.restarted);
        assert_eq!(r.outcome, "restarted");
    }
}
