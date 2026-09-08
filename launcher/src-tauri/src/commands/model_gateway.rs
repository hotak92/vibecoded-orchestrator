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

/// Ports the starter falls back to when the resolved one is taken by
/// something that is not a gateway.
///
/// The 2026-09-08 machine is the case this exists for: a legacy scorer
/// container owned 11436, so every start attempt died on a bind error the
/// GUI could only report as "it did not come up". Nine ports is enough for
/// any plausible number of local services and small enough to stay a
/// documented, predictable range rather than a scan.
///
/// It sits at 11460, clear of every port a VCO service claims, because a
/// fallback must never hand the gateway a port ANOTHER VCO SERVICE OWNS.
/// `port_is_free` only asks whether a port is bindable right now, so taking
/// a reserved-but-idle one does not fail here — it fails later, when that
/// service starts and cannot bind its own address. The claimed ports
/// (v0.2.94 survey of shipped code):
///
/// * 11435 — Ollama (`mcp_registration.rs::DEFAULT_OLLAMA_PORT`).
/// * 11436 — this gateway's own default ([`DEFAULT_GATEWAY_PORT`]).
/// * 11438 — RL container-internal `RL_SERVER_PORT`.
/// * 11439 — legacy RL server (`weaviate_mcp/server.py`'s `RL_SERVER_URL`).
/// * 11440 — code-embed service (`vco_lib/code_embed_image.py::DEFAULT_PORT`).
/// * 11442 — `module_service.rs::ORCHESTRATOR_ROOT_RL_PORT`.
/// * 11443 — `container_runtime.rs::GLOBAL_RL_PORT`.
/// * 11450 — module-manifest container-port example.
/// * 11500..=11900 — per-project RL allocation window
///   (`module_service.rs::RL_PORT_RANGE_LO`/`_HI`).
///
/// CROSS-LANE (v0.2.94): the gateway daemon retries the SAME nine ports on
/// EADDRINUSE, as `model_router.config.FALLBACK_PORT_RANGE`.
/// `tests/test_v0292_model_gateway_gui_contract.py` compares the two the
/// moment that constant exists, and guards the not-yet-landed state until
/// then. Moving the range means moving BOTH sides in the same change.
pub const FALLBACK_PORT_RANGE: std::ops::RangeInclusive<u16> = 11460..=11468;

/// `/health`'s `service` value. MUST MATCH `model_router/server.py`. A port
/// answering with anything else is NOT a gateway, however plausible.
const GATEWAY_SERVICE: &str = "vct-model-gateway";
const PID_BASENAME: &str = "model-gateway.pid";
const PORT_BASENAME: &str = "model-gateway.port";
/// The launcher's record of the port it last STARTED a gateway on.
///
/// Review R2-2: the daemon unlinks its own port file on a clean exit, so a
/// gateway that had moved off the default (because something else held it)
/// was forgotten the moment it stopped — and the next resolution answered
/// with the shipped default, which on the reporter's machine is a legacy
/// container. Not cosmetic: the uninstall reset then walks past the panel it
/// should clean up, and a Services "point" writes our host token into a base
/// URL naming somebody else's service. This file is written on every start
/// and never deleted; it remembers INTENT, which is exactly what a stopped
/// gateway needs to stay recognisable. MUST MATCH
/// `vco_lib/vscode_settings.py::LAST_PORT_BASENAME`.
const LAST_PORT_BASENAME: &str = "model-gateway.last-port";
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

fn last_port_path() -> PathBuf {
    vct_root_dir().join(LAST_PORT_BASENAME)
}

/// Read a one-line port file. `None` for absent, unreadable, unparseable or
/// out-of-range — a corrupt file must degrade to the next source, never
/// blank the card or resolve to something nonsensical.
fn read_port_file(path: &Path) -> Option<u16> {
    let text = std::fs::read_to_string(path).ok()?;
    let port = text.trim().parse::<u16>().ok()?;
    (port > 0).then_some(port)
}

/// Record the port we just started a gateway on. Soft-fail by design: a
/// gateway that started must not be reported as failed because a memo could
/// not be written. Owner-only through `boot_token::write_token_file`, which
/// is this workspace's ONE implementation of the O_CREAT|0o600 (plus Windows
/// DACL) small-file write — a second copy of that sequence is exactly what
/// the modularity rule forbids, even for a value that is not a secret.
///
/// WRITTEN BEFORE THE SPAWN, AND NOT CLEARED WHEN THE SPAWN FAILS. Both are
/// deliberate. Before, because a daemon that starts and exits between here
/// and the status read deletes its own port file and would otherwise leave
/// nothing behind. Not cleared, because this file records INTENT, not
/// liveness: every consumer re-probes `/health` before believing anything is
/// there, and "ours" is decided by the RESOLVED port alone — so a stale
/// record can at worst make a probe ask about a port nobody is listening on,
/// which answers `stopped`. The alternative (clear it on failure) would
/// reintroduce the very gap this file closes, since a start that fails
/// AFTER binding is indistinguishable from one that never bound.
fn remember_last_port(port: u16) {
    let path = last_port_path();
    if let Err(e) = vct_launcher_core::services::boot_token::write_token_file(
        &path,
        &format!("{}\n", port),
    ) {
        tracing::warn!(
            "[vct] model gateway: could not record the chosen port in {}: {}",
            path.display(),
            e
        );
    }
}

fn token_path() -> PathBuf {
    vct_root_dir().join(TOKEN_BASENAME)
}

/// `$VCT_MODEL_GATEWAY_PORT` -> the daemon's port file -> the launcher's
/// last-chosen-port record -> the documented default.
///
/// The first two steps are `model_router.config.resolve_port`'s order; the
/// third is this launcher's own memory (review R2-2), and it is what keeps a
/// gateway that ran on a fallback port recognisable after the daemon has
/// exited and deleted its port file. Each step is EVIDENCE; the default is
/// the answer only when there is none. MUST MATCH the chain in
/// `vco_lib/vscode_settings.py::resolve_gateway_ports`.
///
/// An out-of-range or unparseable value falls through rather than erroring:
/// these files are read on every status poll, so a corrupt one must degrade
/// to the next source instead of blanking the card.
pub fn resolve_port() -> u16 {
    if let Ok(raw) = std::env::var(PORT_ENV) {
        if let Ok(v) = raw.trim().parse::<u16>() {
            if v > 0 {
                return v;
            }
        }
    }
    read_port_file(&port_path())
        .or_else(|| read_port_file(&last_port_path()))
        .unwrap_or(DEFAULT_GATEWAY_PORT)
}

fn base_url(port: u16) -> String {
    format!("http://127.0.0.1:{}", port)
}

/// The base URL a panel write must carry: the caller's KNOWN port when it has
/// one, the resolved port otherwise.
///
/// Review R1-1b — one home for the decision both panel-writing commands make.
/// A caller that has just started a gateway knows which port it bound, and
/// `resolve_port()` may not: the daemon writes its port file during start-up,
/// and a launcher whose env pins a port reads the pin regardless. Deriving
/// the URL from a stale resolution is how the panel was pointed at a legacy
/// container on 11436 — with our token, under a notice that said "Applied".
pub(crate) fn resolved_base_url(port: Option<u16>) -> String {
    base_url(port.filter(|p| *p > 0).unwrap_or_else(resolve_port))
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
/// `PYTHONPATH` for a `-m` spawn out of the orchestrator clone: the clone
/// root (so `vco_lib` resolves) and `claude_mcp_servers` (so `model_router`
/// does), separated the OS's way.
///
/// One home for both spawns — review R1-4. `run_vscode_settings` did NOT set
/// it while `gateway_command` did, and the consequence was not an import
/// error: `vco_lib.vscode_settings.resolve_gateway_ports` SWALLOWS a failed
/// `model_router` import and answers with the documented default port, so on
/// an install whose `pip install -e claude_mcp_servers/` had not run, a
/// gateway on 11437 read as an unmanaged prototype endpoint. A silent wrong
/// answer from a missing env var is exactly the shape that must have one
/// definition, not two.
pub(crate) fn orchestrator_pythonpath(root: &Path) -> std::ffi::OsString {
    let sep = if cfg!(windows) { ";" } else { ":" };
    let mut value = std::ffi::OsString::from(root.as_os_str());
    value.push(sep);
    value.push(root.join("claude_mcp_servers").as_os_str());
    value
}

/// `python -m <module>` with the launcher's env sandbox and the clone's
/// `PYTHONPATH`.
///
/// `PYTHONPATH` entries precede site-packages in `sys.path`, so THE CLONE
/// WINS over any installed distribution of the same name (review R2-7 — this
/// comment previously claimed the opposite). That is the intended
/// resolution: the launcher spawns the source it was built from, and it also
/// keeps a not-yet-`pip install -e`d clone working instead of degrading.
fn python_module_command(python: &Path, module: &str, orchestrator_root: Option<&Path>) -> Command {
    let mut cmd = Command::new(python).silent();
    cmd.arg("-m").arg(module);
    crate::services::vco_lib_bridge::reinject_minimal_env(&mut cmd);
    // The gateway's documented knobs, which the sandbox's allowlist (built
    // for `vco_lib` spawns) does not carry. BOTH spawns need them, not just
    // the daemon's — review R2-6: the writer resolves the gateway's port,
    // and `VCT_MODEL_GATEWAY_PORT` is exactly the pin that answer must
    // honour. Without this it never saw the pin and answered from files
    // alone, disagreeing with the launcher that spawned it.
    for (k, v) in std::env::vars() {
        if k.starts_with(GATEWAY_ENV_PREFIX) {
            cmd.env(k, v);
        }
    }
    if let Some(root) = orchestrator_root {
        cmd.env("PYTHONPATH", orchestrator_pythonpath(root));
    }
    cmd
}

fn gateway_command(python: &Path, orchestrator_root: Option<&Path>) -> Command {
    python_module_command(python, "model_router", orchestrator_root)
}

/// The writer spawn. Same `PYTHONPATH` as the daemon spawn (review R1-4):
/// the writer imports `model_router` to resolve the gateway's port and its
/// context table, and answers with a DEFAULT rather than an error when it
/// cannot.
pub(crate) fn vscode_settings_command(python: &Path, orchestrator_root: Option<&Path>) -> Command {
    python_module_command(python, "vco_lib.vscode_settings", orchestrator_root)
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
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = vscode_settings_command(&python, root.as_deref());
    for a in args {
        cmd.arg(a);
    }
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

// ─── Port collision ───────────────────────────────────────────────────────

/// Can a listener bind this loopback port right now?
///
/// A bind attempt, not a connect: "nothing answered" and "nothing can bind"
/// are different questions, and only the second one predicts whether the
/// daemon we are about to spawn will come up. The listener is dropped
/// immediately, so this leaves nothing behind.
pub(crate) fn port_is_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// The first port the daemon can actually bind: the requested one, else the
/// first free port in [`FALLBACK_PORT_RANGE`].
///
/// Pure, with the probe injected, so both halves are unit-testable without
/// occupying a real port: the act (fall back) and the leave-alone (a free
/// requested port is used unchanged, never "helpfully" moved).
pub(crate) fn choose_start_port<F>(requested: u16, is_free: F) -> Option<u16>
where
    F: Fn(u16) -> bool,
{
    if is_free(requested) {
        return Some(requested);
    }
    FALLBACK_PORT_RANGE
        .filter(|p| *p != requested)
        .find(|p| is_free(*p))
}

/// Is a VCO gateway already answering here? (As opposed to some other
/// service owning the port, which is the case we route around.)
async fn gateway_answers_on(port: u16) -> bool {
    matches!(probe_health(port).await, (Some(true), Some(h), _) if h.service == GATEWAY_SERVICE)
}

/// How long `model_gateway_start` waits for the daemon to answer, and how
/// often it asks. 5 s total: a cold aiohttp import on a slow disk takes a
/// second or two, and anything past five is a start that failed.
const START_POLL_ATTEMPTS: u32 = 50;
const START_POLL_INTERVAL: Duration = Duration::from_millis(100);

/// Poll `probe` until it answers true, or the attempts run out.
///
/// Review R1-1a: the previous code slept a fixed 600 ms and then REPORTED,
/// which is a guess wearing a status payload's clothes. It also raced the
/// daemon's own start-up order — the token file is written before the port
/// file (`model_router/__main__.py` :184 vs :201) — so a "started" report
/// could precede the port file that every later port resolution reads.
/// Waiting for `/health` waits for both.
///
/// `interval` is a parameter (not a constant) so the unit tests can drive
/// the loop with no wall-clock cost.
pub(crate) async fn poll_until<F, Fut>(attempts: u32, interval: Duration, mut probe: F) -> bool
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    for attempt in 0..attempts {
        if probe().await {
            return true;
        }
        if attempt + 1 < attempts && !interval.is_zero() {
            tokio::time::sleep(interval).await;
        }
    }
    false
}

/// `VCT_MODEL_GATEWAY_PORT` as the LAUNCHER sees it, when it is a usable port.
fn env_pinned_port() -> Option<u16> {
    std::env::var(PORT_ENV)
        .ok()
        .and_then(|raw| raw.trim().parse::<u16>().ok())
        .filter(|p| *p > 0)
}

/// Why a start must be refused rather than moved off an explicit pin.
///
/// Review R1-1: a pin is a statement about where this machine's gateway
/// lives, and every later resolution — the status poll, the panel's
/// `--base-url`, the MCP env — reads that pin, not our choice. Quietly
/// starting somewhere else would leave every one of them pointing at
/// whatever holds the pinned port. Pure so both halves are testable.
/// `requested` is the port the CALLER asked for explicitly (`None` when the
/// port was resolved rather than named), and it is what distinguishes the two
/// ways a pin can be contradicted — review R2-8: claiming "that port is in
/// use" when the user simply typed a different number is a false diagnosis,
/// and it sends them looking for a process that does not exist.
pub(crate) fn env_pin_conflict(
    pinned: Option<u16>,
    chosen: u16,
    requested: Option<u16>,
) -> Option<String> {
    let p = pinned.filter(|p| *p != chosen)?;
    Some(match requested {
        Some(asked) if asked != p => format!(
            "you asked for port {}, but {} pins port {}. Every later port \
             resolution reads the pin, so the launcher would then look for \
             the gateway on {} while it listened on {}. Change the pin, or \
             start it on {}.",
            asked, PORT_ENV, p, p, asked, p
        ),
        _ => format!(
            "{} pins port {}, but that port is in use by another process and \
             the gateway would have to start on {} instead. Every later port \
             resolution reads the pin, so it would then look for the gateway \
             on {}. Free port {}, or change the pin.",
            PORT_ENV, p, chosen, p, p
        ),
    })
}

// ─── Commands ─────────────────────────────────────────────────────────────

#[command]
pub async fn model_gateway_status(
    supervisor: State<'_, GatewaySupervisor>,
) -> Result<ModelGatewayStatus, String> {
    status_on_port(&supervisor, resolve_port()).await
}

/// The status payload for ONE known port.
///
/// Split out for `model_gateway_start`: the daemon writes its port file as
/// it boots, so a status read microseconds later can still resolve the OLD
/// port and report the just-started gateway as unreachable. The starter
/// knows which port it chose and says so; every other caller resolves it the
/// normal way ([`resolve_port`]: env pin, the port file the daemon wrote,
/// the launcher's last-started-port record, then the default).
async fn status_on_port(
    supervisor: &GatewaySupervisor,
    port: u16,
) -> Result<ModelGatewayStatus, String> {
    let supervised_pid = supervisor.poll();
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

    if port == Some(0) {
        return Err("port 0 is not a valid gateway port".to_string());
    }
    let requested = port.unwrap_or_else(resolve_port);
    // Something already owns the port? Two very different cases, and
    // conflating them is how a start silently does nothing: OUR gateway
    // answering there means there is nothing to start, while any other
    // occupant means we move rather than die on a bind error (the
    // 2026-09-08 machine had a legacy container on 11436).
    let chosen = if port_is_free(requested) {
        requested
    } else if gateway_answers_on(requested).await {
        return Err(format!(
            "a model gateway is already answering on {} — it was not started \
             by this launcher, so there is nothing to start. Use it, or stop \
             it where it was started.",
            base_url(requested)
        ));
    } else {
        match tauri::async_runtime::spawn_blocking(move || {
            choose_start_port(requested, port_is_free)
        })
        .await
        .unwrap_or(None)
        {
            Some(p) => p,
            None => {
                return Err(format!(
                    "port {} is in use by another process, and every fallback \
                     port ({}..={}) is taken too. Free one of them, or set \
                     {} to a port you know is free.",
                    requested,
                    FALLBACK_PORT_RANGE.start(),
                    FALLBACK_PORT_RANGE.end(),
                    PORT_ENV
                ))
            }
        }
    };

    if let Some(conflict) = env_pin_conflict(env_pinned_port(), chosen, port) {
        return Err(conflict);
    }

    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_command(&python, root.as_deref());
    cmd.arg("--port").arg(chosen.to_string());
    // The daemon writes the port file from its own resolution, and the
    // status poll reads it back; pin the env too so a probe issued
    // before the daemon has written the file still asks the right port.
    cmd.env(PORT_ENV, chosen.to_string());
    // Remembered BEFORE the spawn: a daemon that starts and exits between
    // here and the status read would otherwise leave nothing behind (its own
    // port file is deleted on a clean exit — review R2-2).
    remember_last_port(chosen);
    if chosen != requested {
        // NOTE for the next reader: when `requested` came from a
        // `VCT_MODEL_GATEWAY_PORT` pin in the LAUNCHER's own environment,
        // later status polls keep resolving that pinned port and will report
        // whatever owns it — not this gateway. That is the pin doing what a
        // pin does; the honest answer then comes from the card's own
        // "something is listening but did not return a gateway health
        // payload". Starting is still better than dying on a bind error.
        tracing::info!(
            "[vct] model gateway: port {} is in use by another process; starting on {} instead",
            requested,
            chosen
        );
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

    // Wait for the daemon to ANSWER, rather than sleeping and assuming
    // (review R1-1a). `/health` answering with our service name is the only
    // evidence that the process bound the port AND wrote its token and port
    // files — which is what every later resolution reads.
    poll_until(START_POLL_ATTEMPTS, START_POLL_INTERVAL, || {
        gateway_answers_on(chosen)
    })
    .await;
    // Reported on the port we CHOSE, not on whatever the port file says yet:
    // the daemon may not have written it, and a status naming the old port
    // would tell the user the start failed. When the poll timed out the
    // status carries that honestly (`reachable`/`health` from a real probe),
    // and the GUI gates its "started on port N" line on it.
    status_on_port(&supervisor, chosen).await
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
    port: Option<u16>,
) -> Result<serde_json::Value, String> {
    // Same `port` contract as `model_gateway_mode_set` — review R1-1b.
    let mut args = vec![
        "point".to_string(),
        "--path".to_string(),
        path,
        "--base-url".to_string(),
        resolved_base_url(port),
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

/// Remove `ANTHROPIC_MODEL` from the panel's env block — that key only.
///
/// The counterpart to the writer's refusal to WRITE a vendor Default: a file
/// that already holds one is the user's, so it is surfaced in the StatusBar
/// with this one-click action rather than deleted behind their back.
#[command]
pub async fn model_gateway_clear_default_model(
    path: String,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_vscode_settings(&["clear-default".to_string(), "--path".to_string(), path])
    })
    .await
    .map_err(|e| format!("clear-default task failed: {}", e))?
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
/// the port ([`resolve_port`]: env pin, the daemon's port file, the
/// launcher's last-started-port record, then the default), so the two ways
/// of pointing the panel cannot disagree. The `remote-control` leg has no
/// use for it and the argv says so by omitting it.
pub(crate) fn mode_set_argv(path: &str, mode: &str, base_url: Option<&str>) -> Vec<String> {
    // (`base_url` is built by the caller from the port it KNOWS — see
    // `model_gateway_mode_set`'s `port` parameter, review R1-1b.)
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
/// `port` is the port the CALLER knows the gateway is on — the one
/// `model_gateway_start` just chose and proved live. It exists because
/// `resolve_port()` cannot answer that question in time: the daemon writes
/// its port file during start-up, and a launcher whose env pins a port reads
/// the pin regardless. Writing `base_url` from a stale resolution is how the
/// panel got pointed at a legacy container on 11436 — with our token, and a
/// notice that said "Applied" (review R1-1b). `None` keeps the old
/// resolution for callers that have no better answer.
#[command]
pub async fn model_gateway_mode_set(
    path: String,
    mode: String,
    port: Option<u16>,
) -> Result<serde_json::Value, String> {
    let mode = validate_mode(&mode)?.to_string();
    let base_url = if mode == "multimodel" {
        Some(resolved_base_url(port))
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

    // ── port collision (B3) ─────────────────────────────────────────────

    #[test]
    fn a_free_requested_port_is_used_unchanged() {
        // LEAVE-ALONE half: the fallback range exists for a taken port and
        // must never "helpfully" move a start that had no problem.
        assert_eq!(choose_start_port(DEFAULT_GATEWAY_PORT, |_| true), Some(DEFAULT_GATEWAY_PORT));
        assert_eq!(choose_start_port(12345, |_| true), Some(12345));
    }

    #[test]
    fn a_taken_port_falls_back_to_the_first_free_one_in_the_range() {
        // The 2026-09-08 machine: a legacy container owns 11436, so the
        // gateway has to start on 11460 instead of dying on a bind error.
        let taken = |p: u16| p != DEFAULT_GATEWAY_PORT;
        assert_eq!(
            choose_start_port(DEFAULT_GATEWAY_PORT, taken),
            Some(*FALLBACK_PORT_RANGE.start())
        );

        // ...and skips further occupied ones rather than stopping at the first.
        // 11463 is INSIDE the range on purpose: a probe naming a port outside
        // it would answer `None` and this assertion would stop testing the
        // skip at all.
        assert!(FALLBACK_PORT_RANGE.contains(&11463), "the premise of this assertion");
        let only_11463_free = |p: u16| p == 11463;
        assert_eq!(choose_start_port(DEFAULT_GATEWAY_PORT, only_11463_free), Some(11463));
    }

    #[test]
    fn everything_taken_reports_rather_than_guessing() {
        assert_eq!(choose_start_port(DEFAULT_GATEWAY_PORT, |_| false), None);
    }

    #[test]
    fn the_requested_port_is_not_retried_inside_the_fallback_range() {
        // Asking for 11463 and finding it taken must not offer 11463 again.
        // The requested port has to be INSIDE the range for this to assert
        // anything: an outside one is skipped by the iteration regardless, so
        // the "not retried" property would hold vacuously.
        assert!(FALLBACK_PORT_RANGE.contains(&11463), "the premise of this test");
        let seen = std::cell::RefCell::new(Vec::<u16>::new());
        let chosen = choose_start_port(11463, |p| {
            seen.borrow_mut().push(p);
            p == 11464
        });
        assert_eq!(chosen, Some(11464));
        assert_eq!(seen.borrow().iter().filter(|p| **p == 11463).count(), 1);
    }

    #[test]
    fn port_is_free_is_a_bind_probe_not_a_guess() {
        // Hold a real ephemeral port and prove the probe says so — the
        // primitive the fallback is built on, tested against a real socket
        // rather than mocked into always agreeing with itself.
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let held = listener.local_addr().unwrap().port();
        assert!(!port_is_free(held), "a bound port must not read as free");
        drop(listener);
        assert!(port_is_free(held), "and it is free again once released");
    }

    #[test]
    fn the_fallback_range_is_the_documented_one() {
        assert_eq!(*FALLBACK_PORT_RANGE.start(), 11460);
        assert_eq!(*FALLBACK_PORT_RANGE.end(), 11468);
        assert!(
            !FALLBACK_PORT_RANGE.contains(&DEFAULT_GATEWAY_PORT),
            "the default port is not its own fallback"
        );
        // The reason it sits at 11460 (v0.2.94): a fallback must not hand the
        // gateway a port another VCO service owns. One entry per claimed
        // port, so a future widening of the window is a red test rather than
        // a service that cannot bind its own address.
        for reserved in [
            11439u16, // legacy RL server
            11440,    // code-embed service
            11442,    // ORCHESTRATOR_ROOT_RL_PORT
            11443,    // GLOBAL_RL_PORT
            11450,    // module-manifest container-port example
            11500,    // RL_PORT_RANGE_LO (per-project allocation window)
        ] {
            assert!(
                !FALLBACK_PORT_RANGE.contains(&reserved),
                "port {} belongs to another VCO service",
                reserved
            );
        }
    }

    #[test]
    fn the_health_service_name_matches_the_gateway_server() {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let py = std::fs::read_to_string(
            repo_root
                .join("claude_mcp_servers")
                .join("model_router")
                .join("server.py"),
        )
        .expect("model_router/server.py readable");
        assert!(
            py.contains(&format!("\"{}\"", GATEWAY_SERVICE)),
            "the starter decides 'is this port MINE?' by a service name the \
             gateway no longer emits"
        );
    }

    // ── R1-1: the port a start CHOSE must reach the panel write ─────────

    #[test]
    fn mode_set_carries_the_chosen_port_not_a_re_resolution() {
        // The BLOCKER: the gateway starts on 11437 (11436 taken) and the
        // panel write derived its base URL from `resolve_port()` — which on
        // that machine still answered 11436, the legacy container. The panel
        // was handed our token pointing at a foreign service, and the notice
        // said "Applied".
        let argv = mode_set_argv("/p/settings.json", "multimodel", Some(&base_url(11437)));
        let i = argv.iter().position(|a| a == "--base-url").expect("--base-url");
        assert_eq!(argv[i + 1], "http://127.0.0.1:11437");
        assert!(!argv[i + 1].contains("11436"));
    }

    #[test]
    fn a_known_port_beats_the_resolver_for_the_panels_base_url() {
        // The BLOCKER's second half: both panel-writing commands go through
        // this, so a started gateway's port reaches the settings file rather
        // than being re-derived from a resolution that can still be stale.
        let g = scratch_root();
        std::fs::write(g.path().join(PORT_BASENAME), "11436\n").unwrap();
        assert_eq!(resolved_base_url(Some(11437)), "http://127.0.0.1:11437");
        // LEAVE-ALONE half: no known port means the old resolution, unchanged.
        assert_eq!(resolved_base_url(None), "http://127.0.0.1:11436");
        assert_eq!(resolved_base_url(Some(0)), "http://127.0.0.1:11436");
    }

    #[test]
    fn an_env_pin_that_differs_from_the_chosen_port_refuses_the_start() {
        // A pin is a statement about where this machine's gateway lives, and
        // every later resolution reads it. Moving off it silently would point
        // the status poll, the panel and the MCP env at the wrong process.
        let refusal = env_pin_conflict(Some(11436), 11437, None).expect("a conflict must refuse");
        assert!(refusal.contains(PORT_ENV), "{}", refusal);
        assert!(refusal.contains("11436") && refusal.contains("11437"), "{}", refusal);
    }

    #[test]
    fn no_pin_or_a_matching_pin_lets_the_start_proceed() {
        // LEAVE-ALONE half: the refusal is for a CONFLICT, not for pinning.
        assert_eq!(env_pin_conflict(None, 11437, None), None);
        assert_eq!(env_pin_conflict(Some(11437), 11437, None), None);
        assert_eq!(env_pin_conflict(Some(11437), 11437, Some(11437)), None);
    }

    #[test]
    fn neither_refusal_message_carries_a_flattened_line_continuation() {
        // Review R3-3: a `\` continuation that was itself inside a generated
        // string collapsed into the Rust literal, so the user read "Every
        // later port              resolution reads the pin". These messages
        // are shipped copy; a run of spaces in one is a defect in it.
        for message in [
            env_pin_conflict(Some(11436), 11437, None).expect("occupied-port refusal"),
            env_pin_conflict(Some(11436), 11440, Some(11440)).expect("explicit-port refusal"),
        ] {
            assert!(
                !message.contains("   "),
                "a run of 3+ spaces in shipped copy: {:?}",
                message
            );
            assert!(!message.contains('\n'), "one line, wrapped by the GUI: {:?}", message);
        }
    }

    #[test]
    fn an_explicit_port_request_is_not_reported_as_an_occupied_port() {
        // Review R2-8: the pin was contradicted by the CALLER, not by a
        // process. Saying "in use by another process" sends the user looking
        // for something that is not there.
        let asked = env_pin_conflict(Some(11436), 11440, Some(11440)).expect("refusal");
        assert!(asked.contains("you asked for port 11440"), "{}", asked);
        assert!(!asked.contains("in use by another process"), "{}", asked);

        let occupied = env_pin_conflict(Some(11436), 11437, None).expect("refusal");
        assert!(occupied.contains("in use by another process"), "{}", occupied);
    }

    // ── R2-2: the last-chosen-port record ───────────────────────────────

    #[test]
    fn the_last_started_port_is_used_once_the_daemon_deleted_its_port_file() {
        // The daemon unlinks its port file on a clean exit; without this
        // record the very next resolution answers with the shipped default,
        // which on the reporter's machine is a legacy container.
        let g = scratch_root();
        std::fs::write(g.path().join(LAST_PORT_BASENAME), "11437\n").unwrap();
        assert_eq!(resolve_port(), 11437);
    }

    #[test]
    fn a_live_port_file_beats_the_last_port_record() {
        let g = scratch_root();
        std::fs::write(g.path().join(PORT_BASENAME), "11440\n").unwrap();
        std::fs::write(g.path().join(LAST_PORT_BASENAME), "11437\n").unwrap();
        assert_eq!(resolve_port(), 11440);
    }

    #[test]
    fn a_corrupt_last_port_record_degrades_to_the_default() {
        let g = scratch_root();
        for bad in ["", "nonsense", "0", "999999"] {
            std::fs::write(g.path().join(LAST_PORT_BASENAME), bad).unwrap();
            assert_eq!(resolve_port(), DEFAULT_GATEWAY_PORT, "{:?}", bad);
        }
    }

    #[test]
    fn remember_last_port_writes_the_file_the_python_side_reads() {
        let g = scratch_root();
        remember_last_port(11437);
        let path = g.path().join(LAST_PORT_BASENAME);
        assert_eq!(
            std::fs::read_to_string(&path).unwrap().trim(),
            "11437",
            "the record is what makes a stopped gateway recognisable"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600, "state under ~/.vct is owner-only");
        }
    }

    #[test]
    fn the_last_port_basename_matches_the_python_reader() {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let py = std::fs::read_to_string(repo_root.join("vco_lib").join("vscode_settings.py"))
            .expect("vco_lib/vscode_settings.py readable");
        assert!(
            py.contains(&format!("LAST_PORT_BASENAME = \"{}\"", LAST_PORT_BASENAME)),
            "the two sides would read and write different files"
        );
    }

    #[test]
    fn both_python_spawns_carry_the_gateway_knobs() {
        // Review R2-6: the writer resolves the gateway's port, and the pin is
        // the first step of that resolution — it must see it.
        let _g = scratch_root();
        let saved = std::env::var_os(PORT_ENV);
        // SAFETY: `scratch_root()` holds the workspace-wide env mutex.
        unsafe { std::env::set_var(PORT_ENV, "11437") };

        let carried: Vec<bool> = [
            gateway_command(Path::new("/usr/bin/python3"), None),
            vscode_settings_command(Path::new("/usr/bin/python3"), None),
        ]
        .iter()
        .map(|cmd| {
            cmd.get_envs().any(|(k, v)| {
                k.to_string_lossy() == PORT_ENV
                    && v.map(|vv| vv.to_string_lossy() == "11437").unwrap_or(false)
            })
        })
        .collect();

        unsafe {
            match saved {
                Some(v) => std::env::set_var(PORT_ENV, v),
                None => std::env::remove_var(PORT_ENV),
            }
        }
        assert_eq!(carried, vec![true, true], "both spawns must see the pin");
    }

    #[test]
    fn the_start_waits_for_a_live_answer_instead_of_sleeping() {
        // Review R1-1a: the daemon writes its TOKEN before its PORT file, so
        // a fixed sleep could report "started" while both the port file and
        // the token were still absent. The poll asks until it gets an answer.
        let calls = std::cell::Cell::new(0u32);
        let live_on_third = || {
            let n = calls.get() + 1;
            calls.set(n);
            async move { n >= 3 }
        };
        let ok = tauri::async_runtime::block_on(poll_until(10, Duration::ZERO, live_on_third));
        assert!(ok);
        assert_eq!(calls.get(), 3, "it stops at the first live answer");
    }

    #[test]
    fn the_start_poll_gives_up_rather_than_hanging() {
        let calls = std::cell::Cell::new(0u32);
        let never = || {
            calls.set(calls.get() + 1);
            async { false }
        };
        let ok = tauri::async_runtime::block_on(poll_until(4, Duration::ZERO, never));
        assert!(!ok, "a gateway that never answers must not report as started");
        assert_eq!(calls.get(), 4);
    }

    #[test]
    fn the_start_poll_budget_is_bounded_and_documented() {
        assert_eq!(START_POLL_ATTEMPTS, 50);
        assert_eq!(START_POLL_INTERVAL, Duration::from_millis(100));
        let total = START_POLL_INTERVAL * START_POLL_ATTEMPTS;
        assert!(total <= Duration::from_secs(6), "a GUI click cannot wait longer");
    }

    // ── R1-4: PYTHONPATH parity between the two python spawns ───────────

    #[test]
    fn both_python_spawns_carry_the_clone_on_pythonpath() {
        // The writer spawn had no PYTHONPATH while the daemon spawn did, and
        // `resolve_gateway_ports` SWALLOWS the resulting ImportError and
        // answers with the default port — so our own gateway on 11437 read as
        // an unmanaged prototype endpoint.
        let root = Path::new("/opt/vco");
        for cmd in [
            gateway_command(Path::new("/usr/bin/python3"), Some(root)),
            vscode_settings_command(Path::new("/usr/bin/python3"), Some(root)),
        ] {
            let value = cmd
                .get_envs()
                .find(|(k, _)| k.to_string_lossy() == "PYTHONPATH")
                .and_then(|(_, v)| v)
                .map(|v| v.to_string_lossy().to_string())
                .expect("PYTHONPATH must be set for a -m spawn out of the clone");
            assert!(value.contains("/opt/vco"), "{}", value);
            assert!(value.contains("claude_mcp_servers"), "{}", value);
        }
    }

    #[test]
    fn the_pythonpath_separator_is_the_platforms_own() {
        let value = orchestrator_pythonpath(Path::new("/opt/vco"))
            .to_string_lossy()
            .to_string();
        let sep = if cfg!(windows) { ";" } else { ":" };
        assert_eq!(value.matches(sep).count(), 1, "{}", value);
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
    fn the_clear_default_subcommand_exists_in_the_python_cli() {
        // The command shells to `clear-default`; argparse rejects an unknown
        // subcommand with a usage message on STDERR and no JSON, which the
        // bridge would report as "did not return JSON" — a drift that reads
        // like a broken interpreter.
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let py = std::fs::read_to_string(repo_root.join("vco_lib").join("vscode_settings.py"))
            .expect("vco_lib/vscode_settings.py readable");
        assert!(py.contains("\"clear-default\""));
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
