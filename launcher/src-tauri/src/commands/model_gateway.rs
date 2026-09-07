// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Tauri command surface for the local model gateway (v0.2.92, WP-12).
//!
//! Three jobs, and they have deliberately different shapes:
//!
//!   1. **Lifecycle** — start / stop / boot-autostart for the
//!      `vct-model-gateway` daemon. Mirrors `hub_launcher.rs` +
//!      `hub_status.rs`: a pid file under `<vct_root>`, a liveness probe
//!      that never lies, and boot registration driven through the daemon's
//!      OWN `--register-boot` / `--unregister-boot` / `--boot-status` flags
//!      (same three words, same exit codes as `vct-hub`, so one code path
//!      reads either).
//!   2. **Status** — a `/health` probe with a short timeout, reported as a
//!      TRI-STATE. "I could not reach it" is its own answer here; collapsing
//!      it into "stopped" would make a hung daemon indistinguishable from an
//!      absent one, and collapsing it into "running" is worse.
//!   3. **Panel wiring** — "point the VS Code panel at the gateway" and the
//!      "native" reset. These do NOT edit anything from Rust: they shell to
//!      `python -m vco_lib.vscode_settings`, which is the byte-layout
//!      authority for that file (the same division `config_projection`
//!      already uses for `.claude/settings.json`). Rust owning a second JSON
//!      serialiser for a user-owned file is exactly the divergence the A>B>C
//!      rule exists to prevent.
//!
//! ## The host token never crosses this process
//!
//! The Python side reads the gateway's own token file. This module never
//! reads it, never holds it, never logs it and never puts it in argv. The
//! `VCT_GW_TMP_TOKEN` env channel exists in the Python CLI for callers that
//! already hold the token; this one does not, which is strictly better —
//! the fewer processes a credential passes through, the fewer places it can
//! be captured.
//!
//! ## Stop, and what it deliberately refuses to do
//!
//! `model_gateway_stop` terminates the gateway ONLY when this launcher
//! started it and still holds the child handle. It does NOT read a pid out
//! of the pid file and signal it: a crashed daemon can leave a stale pid
//! file behind, the OS reuses pids, and the repo's standing rule for
//! best-effort paths is to do nothing rather than guess when process
//! identity cannot be positively confirmed. A gateway started by the boot
//! unit or from a terminal reports as unsupervised, and the GUI says how to
//! stop it instead of pretending it can.
//!
//! The complete fix is a `vct-model-gateway --stop` flag next to the
//! existing `--register-boot` family (the daemon can identify itself with
//! certainty); that file is outside this package's file set and the recipe
//! is in its report.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use vct_launcher_core::paths::vct_root_dir;
use vct_launcher_core::process::pid_is_alive;
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::python_resolve::resolve_python_for_vco_lib;

// ─── Constants mirrored from the gateway package ──────────────────────────
//
// MUST MATCH `claude_mcp_servers/model_router/config.py`: `DEFAULT_PORT`,
// `_PID_BASENAME`, `_PORT_BASENAME`, `_TOKEN_BASENAME`, and the
// `VCT_MODEL_GATEWAY_PORT` env name. Held in lockstep by
// `tests/test_v0292_model_gateway_gui_contract.py`, which reads both source
// files and compares the literals.
//
// This is a deliberate (C)-tier mirror under the repo's A>B>C rule, for the
// same reason `chat_model_context.rs` carries one: the alternative is
// shelling to Python to resolve a filename on every 5-second status poll,
// which would make the launcher's status card depend on the gateway package
// being importable — backwards, since the card's whole job includes
// reporting that the gateway is NOT installed.

/// The gateway's documented default port.
pub const DEFAULT_GATEWAY_PORT: u16 = 11436;
const PID_BASENAME: &str = "model-gateway.pid";
const PORT_BASENAME: &str = "model-gateway.port";
const TOKEN_BASENAME: &str = "model-gateway.token";
const PORT_ENV: &str = "VCT_MODEL_GATEWAY_PORT";

/// Env prefix whose keys are forwarded into a gateway we spawn. The daemon's
/// documented knobs (`VCT_MODEL_GATEWAY_CREDENTIALS`,
/// `..._SECRET_PROJECT`, the cache TTLs) are user-set and must survive the
/// env sandbox, or setting one and starting from the GUI would silently do
/// nothing.
const GATEWAY_ENV_PREFIX: &str = "VCT_MODEL_GATEWAY_";

/// Wall-clock cap on a `python -m vco_lib.vscode_settings` call. The happy
/// path is a few tens of milliseconds; anything past this is a wedged
/// interpreter and is better surfaced than left spinning under the user's
/// click. It is also what makes reading the child's pipes AFTER exit safe:
/// a child that filled a pipe buffer and blocked is killed here rather than
/// deadlocking the caller.
const PY_TIMEOUT: Duration = Duration::from_secs(30);

/// `/health` probe timeout. Short on purpose — this runs on a GUI poll.
const HEALTH_TIMEOUT: Duration = Duration::from_millis(1500);

// ─── Supervised-child registry ────────────────────────────────────────────

/// Handles for gateways THIS launcher started. Registered as Tauri state in
/// `lib.rs`; empty after a launcher restart, which is exactly why
/// `supervised` is reported to the GUI rather than assumed.
#[derive(Default)]
pub struct GatewaySupervisor(Mutex<Option<Child>>);

impl GatewaySupervisor {
    /// Reap an exited child so a long-lived launcher does not accumulate a
    /// zombie, and report whether a live supervised child remains.
    fn poll(&self) -> Option<u32> {
        let mut guard = self.0.lock().ok()?;
        let child = guard.as_mut()?;
        match child.try_wait() {
            Ok(Some(_)) => {
                *guard = None;
                None
            }
            Ok(None) => Some(child.id()),
            Err(_) => Some(child.id()),
        }
    }
}

// ─── Paths and port resolution ────────────────────────────────────────────

fn pid_path() -> PathBuf {
    vct_root_dir().join(PID_BASENAME)
}

fn port_path() -> PathBuf {
    vct_root_dir().join(PORT_BASENAME)
}

fn token_path() -> PathBuf {
    vct_root_dir().join(TOKEN_BASENAME)
}

/// `$VCT_MODEL_GATEWAY_PORT` -> the port file -> the documented default.
///
/// Same order as `model_router.config.resolve_port`. An out-of-range or
/// unparseable value falls through rather than erroring: the port file is
/// written by the daemon and read here, so a corrupt one must degrade to the
/// default instead of blanking the status card.
pub fn resolve_port() -> u16 {
    if let Ok(raw) = std::env::var(PORT_ENV) {
        if let Ok(v) = raw.trim().parse::<u16>() {
            if v > 0 {
                return v;
            }
        }
    }
    if let Ok(text) = std::fs::read_to_string(port_path()) {
        if let Ok(v) = text.trim().parse::<u16>() {
            if v > 0 {
                return v;
            }
        }
    }
    DEFAULT_GATEWAY_PORT
}

fn base_url(port: u16) -> String {
    format!("http://127.0.0.1:{}", port)
}

// ─── Process state ────────────────────────────────────────────────────────

/// Tri-state, plus the one state a pid file can be in that neither "running"
/// nor "stopped" describes honestly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessState {
    Running,
    /// Pid file present, the process it names is gone. The daemon removes
    /// its own pid file on a clean exit, so this means it crashed or was
    /// killed — worth showing, because it is also the state in which the
    /// gateway refuses to start until the file is dealt with.
    StalePidFile,
    NotRunning,
}

impl ProcessState {
    fn as_str(self) -> &'static str {
        match self {
            ProcessState::Running => "running",
            ProcessState::StalePidFile => "stale_pid_file",
            ProcessState::NotRunning => "not_running",
        }
    }
}

/// Read the pid file and classify. Never fails; an unreadable or unparseable
/// file is `NotRunning` (the daemon overwrites it on its next start).
pub fn probe_process() -> (ProcessState, Option<u32>) {
    let Ok(raw) = std::fs::read_to_string(pid_path()) else {
        return (ProcessState::NotRunning, None);
    };
    let Some(first) = raw.lines().next() else {
        return (ProcessState::NotRunning, None);
    };
    let Ok(pid) = first.trim().parse::<u32>() else {
        return (ProcessState::NotRunning, None);
    };
    if pid_is_alive(pid) {
        (ProcessState::Running, Some(pid))
    } else {
        (ProcessState::StalePidFile, Some(pid))
    }
}

// ─── /health ──────────────────────────────────────────────────────────────

/// The gateway's `/health` payload.
///
/// Every field carries `#[serde(default)]` so a gateway one version ahead or
/// behind this launcher still parses. A status card that renders nothing
/// because one field was added upstream is worse than one that renders a
/// blank for it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GatewayHealth {
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub service: String,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub host: String,
    /// vendor family -> `live` / `static` / `unfetched` / `unavailable`.
    /// Surfaced verbatim: a picker silently served from the static fallback
    /// is a thing the user must be able to see.
    #[serde(default)]
    pub catalog_source: HashMap<String, String>,
    #[serde(default)]
    pub context_table_source: String,
    #[serde(default)]
    pub context_table_path: Option<String>,
    #[serde(default)]
    pub oauth_present: bool,
    #[serde(default)]
    pub oauth_state: String,
    #[serde(default)]
    pub vendors: Vec<String>,
    #[serde(default)]
    pub vendor_keys_cached: Vec<String>,
    /// `owner_only` / `broader` / `unknown` for the gateway's token file.
    #[serde(default)]
    pub token_file_permissions: String,
}

// ─── Status payload ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct ModelGatewayStatus {
    /// `running` / `stale_pid_file` / `not_running`.
    pub process: String,
    pub pid: Option<u32>,
    /// True when THIS launcher session started the gateway and still holds
    /// the child handle — the only case in which stopping it is safe.
    pub supervised: bool,
    pub port: u16,
    pub base_url: String,
    /// `Some(true)` reachable, `Some(false)` refused, `None` = could not
    /// determine (timeout, unreadable body). Never collapsed.
    pub reachable: Option<bool>,
    pub health: Option<GatewayHealth>,
    pub health_error: Option<String>,
    /// `enabled` / `disabled` / `unsupported` — the daemon's own
    /// `--boot-status` contract words.
    pub boot: String,
    /// The host token exists on disk, i.e. the gateway has run at least
    /// once. Presence only; the value is never read here.
    pub token_present: bool,
    /// The launcher can resolve an interpreter to run the daemon with.
    pub python: Option<String>,
}

// ─── Health probe ─────────────────────────────────────────────────────────

async fn probe_health(port: u16) -> (Option<bool>, Option<GatewayHealth>, Option<String>) {
    let client = match reqwest::Client::builder().timeout(HEALTH_TIMEOUT).build() {
        Ok(c) => c,
        Err(e) => return (None, None, Some(format!("http client: {}", e))),
    };
    let url = format!("{}/health", base_url(port));
    match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => match resp.json::<GatewayHealth>().await {
            Ok(h) => (Some(true), Some(h), None),
            // Something answered on the port but did not speak our shape.
            // That is NOT "the gateway is up", and it is not "nothing is
            // there" either.
            Err(e) => (
                None,
                None,
                Some(format!(
                    "something is listening on {} but did not return a \
                     gateway health payload ({})",
                    url, e
                )),
            ),
        },
        Ok(resp) => (
            Some(false),
            None,
            Some(format!("{} returned HTTP {}", url, resp.status().as_u16())),
        ),
        Err(e) if e.is_connect() => (Some(false), None, Some("connection refused".to_string())),
        Err(e) if e.is_timeout() => (
            None,
            None,
            Some(format!("no answer within {} ms", HEALTH_TIMEOUT.as_millis())),
        ),
        Err(e) => (None, None, Some(format!("{}", e))),
    }
}

// ─── Spawning the daemon's own CLI ────────────────────────────────────────

/// Build `python -m model_router …` with the launcher's env sandbox.
///
/// `PYTHONPATH` is appended so the spawn also works on an install whose
/// `pip install -e claude_mcp_servers/` did not run (or ran into a broken
/// venv): `model_router` is importable from the clone directly. That is a
/// hint, not a second resolver — the installed distribution still wins.
fn gateway_command(python: &Path, orchestrator_root: Option<&Path>) -> Command {
    let mut cmd = Command::new(python).silent();
    cmd.arg("-m").arg("model_router");
    crate::services::vco_lib_bridge::reinject_minimal_env(&mut cmd);
    // Re-inject the daemon's own documented knobs, which the sandbox's
    // allowlist (built for `vco_lib` spawns) does not carry.
    for (k, v) in std::env::vars() {
        if k.starts_with(GATEWAY_ENV_PREFIX) {
            cmd.env(k, v);
        }
    }
    if let Some(root) = orchestrator_root {
        cmd.env("PYTHONPATH", root.join("claude_mcp_servers"));
    }
    cmd
}

fn python_or_err() -> Result<PathBuf, String> {
    resolve_python_for_vco_lib().ok_or_else(|| {
        "no python interpreter found for the model gateway (checked \
         $VCT_VENV, <VCT_INSTALL_ROOT>/.venv, \
         <VCT_INSTALL_ROOT>/claude_mcp_servers/.venv, then PATH). \
         Re-run install.py to rebuild the orchestrator venv."
            .to_string()
    })
}

/// Run a short gateway CLI invocation and return `(exit_code, stdout, stderr)`.
fn run_gateway_cli(args: &[&str]) -> Result<(i32, String, String), String> {
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_command(&python, root.as_deref());
    for a in args {
        cmd.arg(a);
    }
    run_to_completion(cmd, "vct-model-gateway")
}

/// Spawn, wait with a deadline, then drain both pipes.
///
/// Draining after exit is safe because of the deadline: a child that filled
/// a pipe buffer never exits, so it is killed here rather than deadlocking
/// the GUI thread. Every payload this module reads is a single small JSON
/// object, far below any platform's pipe capacity.
fn run_to_completion(mut cmd: Command, label: &str) -> Result<(i32, String, String), String> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("{}: spawn failed: {}", label, e))?;

    let deadline = Instant::now() + PY_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "{}: timed out after {} s",
                        label,
                        PY_TIMEOUT.as_secs()
                    ));
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(e) => return Err(format!("{}: wait failed: {}", label, e)),
        }
    };

    let mut stdout = String::new();
    let mut stderr = String::new();
    if let Some(mut s) = child.stdout.take() {
        use std::io::Read;
        let _ = s.read_to_string(&mut stdout);
    }
    if let Some(mut s) = child.stderr.take() {
        use std::io::Read;
        let _ = s.read_to_string(&mut stderr);
    }
    Ok((status.code().unwrap_or(-1), stdout, stderr))
}

// ─── vco_lib.vscode_settings bridge ───────────────────────────────────────

/// Run `python -m vco_lib.vscode_settings <args>` and parse its stdout JSON.
///
/// The CLI's stdout is a machine contract (one JSON object, nothing else);
/// human notes go to stderr. A non-zero exit is NOT an error here when the
/// payload parses — a refusal is a legitimate, structured outcome the GUI
/// must render, not an exception to swallow.
fn run_vscode_settings(args: &[String]) -> Result<serde_json::Value, String> {
    let python = python_or_err()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m").arg("vco_lib.vscode_settings");
    for a in args {
        cmd.arg(a);
    }
    crate::services::vco_lib_bridge::reinject_minimal_env(&mut cmd);
    let (code, stdout, stderr) = run_to_completion(cmd, "vco_lib.vscode_settings")?;
    match serde_json::from_str::<serde_json::Value>(stdout.trim()) {
        Ok(v) => Ok(v),
        Err(e) => Err(format!(
            "vco_lib.vscode_settings exited {} and did not return JSON ({}): {}",
            code,
            e,
            stderr.trim()
        )),
    }
}

// ─── Commands ─────────────────────────────────────────────────────────────

#[command]
pub async fn model_gateway_status(
    supervisor: State<'_, GatewaySupervisor>,
) -> Result<ModelGatewayStatus, String> {
    let supervised_pid = supervisor.poll();
    let port = resolve_port();
    let (state, pid) = probe_process();
    let (reachable, health, health_error) = probe_health(port).await;
    let boot = tauri::async_runtime::spawn_blocking(boot_status_word)
        .await
        .unwrap_or_else(|_| "unsupported".to_string());

    Ok(ModelGatewayStatus {
        process: state.as_str().to_string(),
        pid: pid.or(supervised_pid),
        supervised: supervised_pid.is_some() && supervised_pid == pid,
        port,
        base_url: base_url(port),
        reachable,
        health,
        health_error,
        boot,
        token_present: token_path().is_file(),
        python: resolve_python_for_vco_lib().map(|p| p.to_string_lossy().to_string()),
    })
}

/// `--boot-status` prints one contract word and exits 0/1/2/3. Anything the
/// launcher cannot classify becomes `unsupported`, which the GUI renders as
/// a disabled toggle with a reason — never as a confident "off".
fn boot_status_word() -> String {
    match run_gateway_cli(&["--boot-status"]) {
        Ok((0, _, _)) => "enabled".to_string(),
        Ok((1, _, _)) | Ok((2, _, _)) => "disabled".to_string(),
        _ => "unsupported".to_string(),
    }
}

#[command]
pub async fn model_gateway_start(
    supervisor: State<'_, GatewaySupervisor>,
    port: Option<u16>,
) -> Result<ModelGatewayStatus, String> {
    if supervisor.poll().is_some() {
        return Err("this launcher already started a model gateway".to_string());
    }
    let (state, pid) = probe_process();
    if state == ProcessState::Running {
        return Err(format!(
            "a model gateway is already running (pid {}). Stop it before \
             starting another — they would contend for the same port.",
            pid.unwrap_or(0)
        ));
    }

    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_command(&python, root.as_deref());
    if let Some(p) = port {
        if p == 0 {
            return Err("port 0 is not a valid gateway port".to_string());
        }
        cmd.arg("--port").arg(p.to_string());
        // The daemon writes the port file from its own resolution, and the
        // status poll reads it back; pin the env too so a probe issued
        // before the daemon has written the file still asks the right port.
        cmd.env(PORT_ENV, p.to_string());
    }
    // The daemon logs to its own file; a detached child must not inherit
    // the GUI's stdio.
    let child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| {
            format!(
                "could not start the model gateway with {}: {}",
                python.display(),
                e
            )
        })?;

    if let Ok(mut guard) = supervisor.0.lock() {
        *guard = Some(child);
    }

    // Give the daemon a moment to bind before the first status read, so the
    // card does not flash "not running" immediately after a successful
    // start. Bounded and short; the poller corrects either way.
    tauri::async_runtime::spawn_blocking(|| std::thread::sleep(Duration::from_millis(600)))
        .await
        .ok();
    model_gateway_status(supervisor).await
}

#[derive(Debug, Serialize)]
pub struct StopOutcome {
    pub stopped: bool,
    pub message: String,
}

#[command]
pub async fn model_gateway_stop(
    supervisor: State<'_, GatewaySupervisor>,
) -> Result<StopOutcome, String> {
    let supervised_pid = supervisor.poll();
    let (state, pid) = probe_process();

    if supervised_pid.is_none() {
        return Ok(match state {
            ProcessState::Running => StopOutcome {
                stopped: false,
                message: format!(
                    "The gateway (pid {}) was not started by this launcher, so \
                     it cannot be stopped from here — a pid read from a file is \
                     not proof of which process it names, and signalling the \
                     wrong one is worse than leaving this button inert. If it \
                     was started at login, turn Start at login off (on Linux \
                     and macOS that also stops it; on Windows it removes the \
                     task but leaves the running process). If it was started \
                     from a terminal, stop it there.",
                    pid.unwrap_or(0)
                ),
            },
            ProcessState::StalePidFile => StopOutcome {
                stopped: false,
                message: format!(
                    "No gateway is running. A stale pid file names pid {}, which \
                     no longer exists; the next start overwrites it.",
                    pid.unwrap_or(0)
                ),
            },
            ProcessState::NotRunning => StopOutcome {
                stopped: true,
                message: "No gateway is running.".to_string(),
            },
        });
    }

    let result = tauri::async_runtime::spawn_blocking({
        // Take the child out of the state, so a failed kill cannot leave a
        // half-owned handle behind.
        let taken = supervisor.0.lock().ok().and_then(|mut g| g.take());
        move || match taken {
            Some(mut child) => match child.kill() {
                Ok(()) => {
                    let _ = child.wait();
                    Ok(())
                }
                Err(e) => Err(format!("could not stop the gateway: {}", e)),
            },
            None => Ok(()),
        }
    })
    .await
    .map_err(|e| format!("stop task failed: {}", e))?;

    result?;
    Ok(StopOutcome {
        stopped: true,
        message: "Model gateway stopped.".to_string(),
    })
}

#[command]
pub async fn model_gateway_set_boot(enabled: bool) -> Result<String, String> {
    let flag = if enabled {
        "--register-boot"
    } else {
        "--unregister-boot"
    };
    let out = tauri::async_runtime::spawn_blocking(move || run_gateway_cli(&[flag]))
        .await
        .map_err(|e| format!("boot task failed: {}", e))??;
    let (code, _stdout, stderr) = out;
    if code == 0 {
        Ok(boot_status_word())
    } else {
        Err(format!(
            "vct-model-gateway {} exited {}: {}",
            flag,
            code,
            stderr.trim()
        ))
    }
}

/// Configuration self-test — `--check`. Surfaced in the card so "why will it
/// not start?" has an answer that is not a log file.
#[command]
pub async fn model_gateway_check() -> Result<String, String> {
    let (code, stdout, stderr) =
        tauri::async_runtime::spawn_blocking(|| run_gateway_cli(&["--check"]))
            .await
            .map_err(|e| format!("check task failed: {}", e))??;
    let body = if stdout.trim().is_empty() {
        stderr
    } else {
        stdout
    };
    if code == 0 {
        Ok(body)
    } else {
        Err(body)
    }
}

// ─── VS Code panel wiring ─────────────────────────────────────────────────

#[command]
pub async fn model_gateway_vscode_targets() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(|| run_vscode_settings(&["detect".to_string()]))
        .await
        .map_err(|e| format!("detect task failed: {}", e))?
}

#[command]
pub async fn model_gateway_vscode_inspect(path: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_vscode_settings(&["inspect".to_string(), "--path".to_string(), path])
    })
    .await
    .map_err(|e| format!("inspect task failed: {}", e))?
}

/// Write the routing keys into VS Code's global settings.
///
/// `model` is `None` unless the user explicitly picked one in the GUI. That
/// is R15 in one parameter: VCO adds the gateway's catalogue to the picker
/// and never decides which model answers.
#[command]
pub async fn model_gateway_point_panel(
    path: String,
    model: Option<String>,
    remove_slot_overrides: Option<bool>,
) -> Result<serde_json::Value, String> {
    let port = resolve_port();
    let mut args = vec![
        "point".to_string(),
        "--path".to_string(),
        path,
        "--base-url".to_string(),
        base_url(port),
    ];
    if let Some(m) = model.filter(|m| !m.trim().is_empty()) {
        args.push("--model".to_string());
        args.push(m);
    }
    if remove_slot_overrides.unwrap_or(false) {
        args.push("--remove-slot-overrides".to_string());
    }
    tauri::async_runtime::spawn_blocking(move || run_vscode_settings(&args))
        .await
        .map_err(|e| format!("point task failed: {}", e))?
}

#[command]
pub async fn model_gateway_reset_native(path: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_vscode_settings(&["reset".to_string(), "--path".to_string(), path])
    })
    .await
    .map_err(|e| format!("reset task failed: {}", e))?
}

// ─── The Multimodel <-> Remote Control switch ─────────────────────────────
//
// Remote Control is endpoint-gated in Claude Code (>= 2.1.196: refused
// whenever `ANTHROPIC_BASE_URL` is not api.anthropic.com), and the
// extension's env block is VS Code machine-scope, so the user has exactly
// one of {gateway picker, Remote Control} at a time. The StatusBar's
// segmented control drives these two commands; the Python CLI's `mode`
// subcommand owns every byte of the decision (what to strip, what to stash,
// what to restore). This side validates the mode word and builds argv —
// nothing else, for the same A>B>C reason as the rest of this file.

/// The two words the switch accepts. MUST MATCH
/// `vco_lib/vscode_settings.py::MODES`; the CLI's argparse `choices`
/// rejects anything else, so a drift here is a refusal, not a silent write.
pub const PANEL_MODES: [&str; 2] = ["multimodel", "remote-control"];

/// `python -m vco_lib.vscode_settings mode --get --path <p>`
pub(crate) fn mode_get_argv(path: &str) -> Vec<String> {
    vec![
        "mode".to_string(),
        "--get".to_string(),
        "--path".to_string(),
        path.to_string(),
    ]
}

/// `python -m vco_lib.vscode_settings mode --set <mode> --path <p>
/// [--base-url <url>]`.
///
/// `--base-url` rides along ONLY for `multimodel` — it is the same value
/// `model_gateway_point_panel` sends, resolved from this process's view of
/// the port (env, then the port file), so the two ways of pointing the
/// panel cannot disagree. The `remote-control` leg has no use for it and
/// the argv says so by omitting it.
pub(crate) fn mode_set_argv(path: &str, mode: &str, base_url: Option<&str>) -> Vec<String> {
    let mut args = vec![
        "mode".to_string(),
        "--set".to_string(),
        mode.to_string(),
        "--path".to_string(),
        path.to_string(),
    ];
    if let Some(url) = base_url {
        args.push("--base-url".to_string());
        args.push(url.to_string());
    }
    args
}

fn validate_mode(mode: &str) -> Result<&str, String> {
    if PANEL_MODES.contains(&mode) {
        Ok(mode)
    } else {
        Err(format!(
            "unknown panel mode {:?}; expected one of {:?}",
            mode, PANEL_MODES
        ))
    }
}

/// `{"mode": "multimodel"|"remote-control"|"unmanaged"|"unparseable", "path",
/// "detail", ...}` — read-only.
#[command]
pub async fn model_gateway_mode_get(path: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || run_vscode_settings(&mode_get_argv(&path)))
        .await
        .map_err(|e| format!("mode get task failed: {}", e))?
}

/// Apply a mode. The write happens immediately; VS Code must be restarted
/// by the user to load it (the GUI says so and never automates that).
#[command]
pub async fn model_gateway_mode_set(
    path: String,
    mode: String,
) -> Result<serde_json::Value, String> {
    let mode = validate_mode(&mode)?.to_string();
    let base_url = if mode == "multimodel" {
        Some(base_url(resolve_port()))
    } else {
        None
    };
    tauri::async_runtime::spawn_blocking(move || {
        run_vscode_settings(&mode_set_argv(&path, &mode, base_url.as_deref()))
    })
    .await
    .map_err(|e| format!("mode set task failed: {}", e))?
}

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::test_env::state_dir_guard_with;

    /// Scratch state root with `VCT_MODEL_GATEWAY_PORT` UNSET, both taken
    /// under one lock acquisition and both restored on drop. Setting the env
    /// var by hand inside the closure would leak into whichever test ran
    /// next in the same binary — the exact defect the shared helper exists
    /// to prevent.
    fn scratch_root() -> vct_launcher_core::test_env::StateDirGuard {
        state_dir_guard_with(&[(PORT_ENV, None)])
    }

    #[test]
    fn port_falls_back_to_the_documented_default() {
        let _g = scratch_root();
        assert_eq!(resolve_port(), DEFAULT_GATEWAY_PORT);
    }

    #[test]
    fn port_file_is_read_when_present() {
        let g = scratch_root();
        std::fs::write(g.path().join(PORT_BASENAME), "11999\n").unwrap();
        assert_eq!(resolve_port(), 11999);
    }

    #[test]
    fn corrupt_port_file_degrades_to_the_default() {
        let g = scratch_root();
        std::fs::write(g.path().join(PORT_BASENAME), "not-a-port\n").unwrap();
        assert_eq!(resolve_port(), DEFAULT_GATEWAY_PORT);
    }

    #[test]
    fn zero_port_file_degrades_to_the_default() {
        let g = scratch_root();
        std::fs::write(g.path().join(PORT_BASENAME), "0\n").unwrap();
        assert_eq!(resolve_port(), DEFAULT_GATEWAY_PORT);
    }

    #[test]
    fn env_port_beats_the_port_file() {
        let g = state_dir_guard_with(&[(PORT_ENV, Some("12345"))]);
        std::fs::write(g.path().join(PORT_BASENAME), "11999\n").unwrap();
        assert_eq!(resolve_port(), 12345);
    }

    #[test]
    fn no_pid_file_is_not_running() {
        let _g = scratch_root();
        assert_eq!(probe_process().0, ProcessState::NotRunning);
    }

    #[test]
    fn unparseable_pid_file_is_not_running() {
        let g = scratch_root();
        std::fs::write(g.path().join(PID_BASENAME), "nonsense\n").unwrap();
        assert_eq!(probe_process().0, ProcessState::NotRunning);
    }

    #[test]
    fn own_pid_reads_as_running() {
        let g = scratch_root();
        std::fs::write(
            g.path().join(PID_BASENAME),
            format!("{}\n", std::process::id()),
        )
        .unwrap();
        let (state, pid) = probe_process();
        assert_eq!(state, ProcessState::Running);
        assert_eq!(pid, Some(std::process::id()));
    }

    #[test]
    fn dead_pid_reads_as_stale_not_as_stopped() {
        let g = scratch_root();
        // pid_is_alive rejects 0 and anything above i32::MAX, so this is a
        // value guaranteed to classify as dead on every OS.
        std::fs::write(g.path().join(PID_BASENAME), "4294967295\n").unwrap();
        let (state, _) = probe_process();
        assert_eq!(
            state,
            ProcessState::StalePidFile,
            "a stale pid file must not be reported as a clean stop"
        );
    }

    #[test]
    fn health_payload_tolerates_unknown_and_missing_fields() {
        // Forward compatibility is the point: a gateway from a newer release
        // must not blank the card.
        let json = r#"{"ok":true,"service":"vct-model-gateway","a_new_field":42}"#;
        let parsed: GatewayHealth = serde_json::from_str(json).unwrap();
        assert!(parsed.ok);
        assert_eq!(parsed.service, "vct-model-gateway");
        assert_eq!(parsed.port, 0, "an absent field defaults, never errors");
        assert!(parsed.catalog_source.is_empty());
    }

    #[test]
    fn health_payload_carries_the_documented_fields() {
        let json = r#"{
            "ok": true, "service": "vct-model-gateway", "version": "0.2.92",
            "port": 11436, "host": "127.0.0.1",
            "catalog_source": {"claude": "live", "zai": "static"},
            "context_table_source": "seed", "context_table_path": null,
            "oauth_present": false, "oauth_state": "absent",
            "vendors": ["zai"], "vendor_keys_cached": [],
            "token_file_permissions": "owner_only"
        }"#;
        let parsed: GatewayHealth = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.catalog_source.get("zai").map(String::as_str), Some("static"));
        assert_eq!(parsed.token_file_permissions, "owner_only");
        assert_eq!(parsed.oauth_state, "absent");
    }

    #[test]
    fn base_url_is_loopback_only() {
        assert_eq!(base_url(11436), "http://127.0.0.1:11436");
        assert!(!base_url(11436).contains("0.0.0.0"));
    }

    #[test]
    fn gateway_knobs_survive_the_env_sandbox() {
        // R24 in miniature: `reinject_minimal_env` was written for `vco_lib`
        // spawns and its allowlist does not carry `VCT_MODEL_GATEWAY_*`.
        // Without the re-injection loop, a user who set a documented knob and
        // pressed Start would get a daemon that silently ignored it.
        let _g = scratch_root();
        let saved = std::env::var_os("VCT_MODEL_GATEWAY_SECRET_PROJECT");
        // SAFETY: `scratch_root()` holds the workspace-wide env mutex, so no
        // other env-mutating test can observe or race this write.
        unsafe { std::env::set_var("VCT_MODEL_GATEWAY_SECRET_PROJECT", "acme") };

        let cmd = gateway_command(Path::new("/usr/bin/python3"), None);
        let forwarded = cmd.get_envs().any(|(k, v)| {
            k.to_string_lossy() == "VCT_MODEL_GATEWAY_SECRET_PROJECT"
                && v.map(|vv| vv.to_string_lossy() == "acme").unwrap_or(false)
        });

        unsafe {
            match saved {
                Some(v) => std::env::set_var("VCT_MODEL_GATEWAY_SECRET_PROJECT", v),
                None => std::env::remove_var("VCT_MODEL_GATEWAY_SECRET_PROJECT"),
            }
        }
        assert!(
            forwarded,
            "a documented gateway knob was dropped by the env sandbox; the \
             GUI Start button would then ignore it"
        );
    }

    #[test]
    fn unrelated_env_still_does_not_leak_into_the_daemon() {
        // LEAVE-ALONE half: the sandbox's whole purpose is that the launcher's
        // own `.claude/env` inheritance does not reach a child.
        let _g = scratch_root();
        let saved = std::env::var_os("KG_COLLECTION");
        // SAFETY: as above — the guard holds the global env mutex.
        unsafe { std::env::set_var("KG_COLLECTION", "SENTINEL") };

        let cmd = gateway_command(Path::new("/usr/bin/python3"), None);
        let leaked = cmd
            .get_envs()
            .any(|(k, _)| k.to_string_lossy() == "KG_COLLECTION");

        unsafe {
            match saved {
                Some(v) => std::env::set_var("KG_COLLECTION", v),
                None => std::env::remove_var("KG_COLLECTION"),
            }
        }
        assert!(!leaked, "KG_COLLECTION leaked into the gateway daemon's env");
    }

    #[test]
    fn supervisor_starts_empty_so_stop_refuses_a_foreign_process() {
        let sup = GatewaySupervisor::default();
        assert!(
            sup.poll().is_none(),
            "a fresh launcher supervises nothing; stop must refuse rather \
             than signal a pid it cannot identify"
        );
    }

    // ── the mode switch: argv is the whole contract with the Python CLI ──

    #[test]
    fn mode_get_argv_shape_is_fixed() {
        assert_eq!(
            mode_get_argv("/home/u/.config/Code/User/settings.json"),
            vec!["mode", "--get", "--path", "/home/u/.config/Code/User/settings.json"]
        );
    }

    #[test]
    fn mode_set_multimodel_argv_carries_the_base_url() {
        assert_eq!(
            mode_set_argv("/p/settings.json", "multimodel", Some("http://127.0.0.1:11436")),
            vec![
                "mode",
                "--set",
                "multimodel",
                "--path",
                "/p/settings.json",
                "--base-url",
                "http://127.0.0.1:11436"
            ]
        );
    }

    #[test]
    fn mode_set_remote_control_argv_has_no_base_url() {
        // LEAVE-ALONE half: the remote-control leg strips the base URL; a
        // `--base-url` here would be an argument nothing reads.
        let v = mode_set_argv("/p/settings.json", "remote-control", None);
        assert_eq!(v, vec!["mode", "--set", "remote-control", "--path", "/p/settings.json"]);
        assert!(!v.iter().any(|a| a == "--base-url"));
    }

    #[test]
    fn mode_argv_never_carries_a_token() {
        // The host token must not cross this process: neither argv builder
        // has a parameter for it, and no argument looks like one.
        for v in [
            mode_get_argv("/p/settings.json"),
            mode_set_argv("/p/settings.json", "multimodel", Some("http://127.0.0.1:11436")),
        ] {
            assert!(!v.iter().any(|a| a.contains("token") || a.contains("TOKEN")), "{:?}", v);
        }
    }

    #[test]
    fn mode_words_are_validated_before_any_spawn() {
        assert_eq!(validate_mode("multimodel"), Ok("multimodel"));
        assert_eq!(validate_mode("remote-control"), Ok("remote-control"));
        for bad in ["", "Multimodel", "remote_control", "unmanaged", "native"] {
            let err = validate_mode(bad).expect_err(bad);
            assert!(err.contains("unknown panel mode"), "{}", err);
        }
    }

    #[test]
    fn mode_words_match_the_python_writer() {
        // (C)-tier mirror of `vco_lib/vscode_settings.py::MODES`; the CLI
        // rejects anything else via argparse `choices`, so drift here is a
        // refusal rather than a silent write — but it is still drift.
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let py = std::fs::read_to_string(repo_root.join("vco_lib").join("vscode_settings.py"))
            .expect("vco_lib/vscode_settings.py readable");
        for word in PANEL_MODES {
            assert!(
                py.contains(&format!("\"{}\"", word)),
                "mode word {:?} not found in the Python writer",
                word
            );
        }
        assert!(py.contains("MODES = (MODE_MULTIMODEL, MODE_REMOTE_CONTROL)"));
    }
}
