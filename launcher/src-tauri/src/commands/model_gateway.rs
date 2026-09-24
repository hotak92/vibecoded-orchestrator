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
// `tests/test_v0292_model_gateway_gui_contract.py`, which reads the source
// files and compares the literals.
//
// This is a deliberate (C)-tier mirror under the repo's A>B>C rule, for the
// same reason `chat_model_context.rs` carries one: the alternative is
// shelling to Python to resolve a filename on every 5-second status poll,
// which would make the launcher's status card depend on the gateway package
// being importable — backwards, since the card's whole job includes
// reporting that the gateway is NOT installed.
//
// v0.2.95: the PORT half of that mirror moved to
// `vct_launcher_core::services::model_gateway_port`, which the hub's gateway
// supervisor and `/services/status` skeleton also use — three Rust copies
// became one, and the parity test against `model_router/config.py` moved with
// them. What stays local below is what only this file needs: the pid/token
// basenames and the start-time fallback range. Re-exported so the many
// call-sites in this module (and `pub` consumers elsewhere in the crate) read
// unchanged.
pub use vct_launcher_core::services::model_gateway_port::{
    base_url, last_port_path, resolve_port, GATEWAY_SERVICE, PORT_ENV,
};
// NOTE-3 cleanup, corrected: these three are referenced ONLY by this
// module's cfg(test) block (22 sites), so an unconditional re-export is an
// unused import in non-test builds while removing it breaks `cargo test`.
// A test-gated import satisfies both gates.
#[cfg(test)]
use vct_launcher_core::services::model_gateway_port::{
    DEFAULT_GATEWAY_PORT, LAST_PORT_BASENAME, PORT_BASENAME,
};

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

const PID_BASENAME: &str = "model-gateway.pid";
const TOKEN_BASENAME: &str = "model-gateway.token";

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

/// Deadline for the dogfood proof, DERIVED rather than picked.
///
/// `vco_lib.vscode_settings` checks its own budget before every leg, so the
/// worst case there is `DOGFOOD_TOTAL_BUDGET_S` (20) plus one full call that
/// started just inside it, `DOGFOOD_CALL_TIMEOUT_S` (8) = 28 s. Add
/// interpreter startup and import (~3 s on a cold page cache) and round up:
/// 45 s. MUST stay above those two constants — a deadline shorter than the
/// proof's own budget kills a run that was about to answer, and the launcher
/// would report "could not run" for a gateway that was being proved.
/// `tests/test_v0294_gateway_dogfood.py::BudgetParityTests` pins the
/// relationship from the Python side.
const DOGFOOD_TIMEOUT: Duration = Duration::from_secs(45);

/// `/health` probe timeout. Short on purpose — this runs on a GUI poll.
const HEALTH_TIMEOUT: Duration = Duration::from_millis(1500);

// ─── Supervised-child registry ────────────────────────────────────────────

/// How long a supervised child that died must stay dead before it is
/// restarted. Not a sleep: the wait is measured across status polls, so the
/// GUI thread never blocks on it. It exists so a gateway that dies during
/// startup (a port taken in the same instant, a state dir on a mount that is
/// still appearing) is retried once the cause has had a moment to clear.
const RESPAWN_BACKOFF: Duration = Duration::from_secs(2);

/// Respawns allowed per launcher session. ONE, deliberately: a single restart
/// covers the transient death, and anything that dies twice is a real fault
/// the user must see rather than a loop this process hides. Continuous
/// supervision is the boot service's job — it has systemd/launchd behind it,
/// including a start-limit — and duplicating that here would be a second
/// supervisor with different rules.
const MAX_RESPAWNS: u8 = 1;

/// A gateway THIS launcher started, and what is needed to bring it back.
struct Supervised {
    /// `None` once the child has exited and been reaped.
    child: Option<Child>,
    /// The port it was started on. A respawn MUST land on the same one: the
    /// port file, the VS Code settings and every resolver that already read
    /// them name that port, so a restart somewhere else is a gateway nobody
    /// can find.
    port: u16,
    /// When the child was found dead — the clock for [`RESPAWN_BACKOFF`].
    dead_since: Option<Instant>,
    respawns: u8,
}

/// Handles for gateways THIS launcher started. Registered as Tauri state in
/// `lib.rs`; empty after a launcher restart, which is exactly why
/// `supervised` is reported to the GUI rather than assumed.
#[derive(Default)]
pub struct GatewaySupervisor(Mutex<Option<Supervised>>);

impl GatewaySupervisor {
    /// Reap an exited child so a long-lived launcher does not accumulate a
    /// zombie, and report whether a live supervised child remains.
    fn poll(&self) -> Option<u32> {
        let mut guard = self.0.lock().ok()?;
        let entry = guard.as_mut()?;
        let child = entry.child.as_mut()?;
        match child.try_wait() {
            Ok(Some(_)) => {
                entry.child = None;
                entry.dead_since = Some(Instant::now());
                None
            }
            Ok(None) => Some(child.id()),
            Err(_) => Some(child.id()),
        }
    }

    /// [`poll`](Self::poll), plus ONE restart of a child that died.
    ///
    /// A gateway the launcher started has no other supervisor: the boot
    /// service is opt-in and off by default, so without this a crash leaves
    /// a card that says "not running" next to a client that has been pointed
    /// at a dead port — the failure is silent until the next request.
    ///
    /// Deliberately driven from the status poll rather than a background
    /// task: it is the moment the launcher already asks "is it alive?", it
    /// carries no timer of its own, and it cannot outlive the window the
    /// user is looking at. Called `supervise` and not `poll` so a caller
    /// that only wants the fact — `model_gateway_stop` — cannot start a
    /// process by asking a question.
    fn supervise(&self) -> Option<u32> {
        if let Some(pid) = self.poll() {
            return Some(pid);
        }
        let mut guard = self.0.lock().ok()?;
        let entry = guard.as_mut()?;
        if entry.child.is_some() || entry.respawns >= MAX_RESPAWNS {
            return None;
        }
        match entry.dead_since {
            Some(at) if at.elapsed() >= RESPAWN_BACKOFF => {}
            // Either it has not been dead long enough, or we never saw it
            // die (an entry with no clock is one `poll` has not reaped yet).
            _ => return None,
        }
        let port = entry.port;
        match spawn_gateway_child(port) {
            Ok(child) => {
                let pid = child.id();
                tracing::warn!(
                    "[vct] model gateway: the child this launcher started \
                     exited; restarted it on port {} (pid {}). A second death \
                     will not be restarted — enable Start at login for real \
                     supervision.",
                    port,
                    pid
                );
                entry.child = Some(child);
                entry.dead_since = None;
                entry.respawns += 1;
                Some(pid)
            }
            Err(e) => {
                tracing::error!(
                    "[vct] model gateway: the child this launcher started \
                     exited and could not be restarted on port {}: {}",
                    port,
                    e
                );
                entry.respawns = MAX_RESPAWNS;
                None
            }
        }
    }
}

impl GatewaySupervisor {
    /// The pid of a LIVE child this launcher holds, or `None`. A question,
    /// not an action: it reaps a dead child but never respawns one (that is
    /// [`supervise`](Self::supervise)'s job, and a staleness check must not
    /// start a process by asking).
    pub(crate) fn held_child_pid(&self) -> Option<u32> {
        self.poll()
    }

    /// Restart the child THIS launcher holds, on the same port — the
    /// post-update "Continue" for a gateway the launcher itself started
    /// (`commands::gateway_freshness`). `Ok(None)` when no live child is held:
    /// nothing is stopped then, because a process this launcher did not start
    /// is never signalled from here (see [`model_gateway_stop`]).
    ///
    /// Same port on purpose, for the reason [`Supervised::port`] gives: the
    /// port file, the VS Code settings and every resolver name it. Spawned
    /// through [`spawn_gateway_child`], the one spawn recipe.
    ///
    /// `expected_pid` is the child the staleness decision was made about
    /// (review R1 F9). The check that proves staleness can take up to 30 s, and
    /// in that window the stale child may die and be respawned by
    /// [`supervise`](Self::supervise) — on the NEW code. Killing "whatever is
    /// held now" would then end the fresh gateway, so a held child whose pid is
    /// not the decided one is left alone and the answer is `Ok(None)`.
    pub(crate) fn restart_held_child(
        &self,
        expected_pid: u32,
    ) -> Result<Option<(u32, u16)>, String> {
        self.restart_held_child_with(expected_pid, spawn_gateway_child)
    }

    /// [`restart_held_child`](Self::restart_held_child) with the spawn
    /// injected, so the stop-then-respawn is testable without starting a real
    /// gateway.
    fn restart_held_child_with<F>(
        &self,
        expected_pid: u32,
        spawn: F,
    ) -> Result<Option<(u32, u16)>, String>
    where
        F: FnOnce(u16) -> Result<Child, String>,
    {
        if self.poll().is_none() {
            return Ok(None);
        }
        let mut guard = self
            .0
            .lock()
            .map_err(|_| "the gateway supervisor lock is poisoned".to_string())?;
        let Some(entry) = guard.as_mut() else {
            return Ok(None);
        };
        // Compared under the SAME lock the stop happens under, so nothing can
        // swap the child between the comparison and the kill.
        if entry.child.as_ref().map(|c| c.id()) != Some(expected_pid) {
            return Ok(None);
        }
        let Some(mut child) = entry.child.take() else {
            return Ok(None);
        };
        // An already-exited child makes `kill` fail harmlessly; `wait` reaps.
        let _ = child.kill();
        let _ = child.wait();
        let port = entry.port;
        match spawn(port) {
            Ok(new_child) => {
                let pid = new_child.id();
                entry.child = Some(new_child);
                entry.dead_since = None;
                Ok(Some((pid, port)))
            }
            Err(e) => {
                // Stopped but not restarted. Record the death so the status
                // poll's single respawn gets its chance, rather than the entry
                // claiming a child that no longer exists.
                entry.dead_since = Some(Instant::now());
                Err(e)
            }
        }
    }
}

/// Who, if anyone, will restart this gateway when it dies.
///
/// Reported rather than inferred by the GUI, and it distinguishes "nothing
/// is watching this" from "we cannot tell" — a card that guesses
/// "supervised" for a hand-started daemon is exactly how a dead gateway goes
/// unnoticed.
fn supervision_word(running: bool, supervised: bool, pid: Option<u32>, boot: &str) -> String {
    if !running {
        return "not_running".to_string();
    }
    if supervised {
        return "launcher".to_string();
    }
    if boot != "enabled" {
        // Positive knowledge, not a guess: autostart is off and the child is
        // not ours, so no supervisor exists for it.
        return "unsupervised".to_string();
    }
    match boot_service_main_pid() {
        Some(main_pid) if Some(main_pid) == pid => "boot_service".to_string(),
        Some(_) => "unsupervised".to_string(),
        // Autostart is on but the unit's own pid could not be read (not
        // systemd, or the tool is missing). Saying "boot_service" here would
        // be the guess this function exists to avoid.
        None => "unknown".to_string(),
    }
}

/// The pid systemd has for the gateway unit, when that question can be asked.
///
/// Linux only, on purpose. `systemctl --user show` answers it exactly; on
/// macOS and Windows the equivalent needs parsing output that is not a
/// contract, so this returns `None` there and the caller reports `unknown`
/// rather than inventing an answer. MUST MATCH `MODEL_GATEWAY_UNIT_NAME` in
/// `vco_lib/boot_service.py` — the unit is written there.
fn boot_service_main_pid() -> Option<u32> {
    if !cfg!(target_os = "linux") {
        return None;
    }
    let out = Command::new("systemctl")
        .silent()
        .args([
            "--user",
            "show",
            "vct-model-gateway.service",
            "--property=MainPID",
            "--value",
        ])
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    match String::from_utf8_lossy(&out.stdout).trim().parse::<u32>() {
        // systemd reports 0 for "no main process", which is not a pid.
        Ok(0) | Err(_) => None,
        Ok(pid) => Some(pid),
    }
}

// ─── Paths and port resolution ────────────────────────────────────────────

fn pid_path() -> PathBuf {
    vct_root_dir().join(PID_BASENAME)
}

// `port_path`, `last_port_path` and `read_port_file` now live in
// `vct_launcher_core::services::model_gateway_port` (v0.2.95); `last_port_path`
// is re-exported at the top of this file for `remember_last_port`, and the
// other two are reached through `resolve_port`, their only remaining caller
// here. They used to be declared here AND, near-identically, in the hub's
// gateway supervisor; the third consumer — `/services/status` — had no copy at
// all and hard-coded the default port instead, which is how a machine whose
// gateway fell back got a health URL nothing served.

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

// `resolve_port` (env pin -> the daemon's port file -> the launcher's
// last-chosen-port record -> the documented default) and `base_url` are
// re-exported from `vct_launcher_core::services::model_gateway_port`; see
// there for the order's rationale and for the parity pin against
// `model_router.config.resolve_port` / `vco_lib.vscode_settings.
// resolve_gateway_ports`.

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
    /// Seconds until the Claude login expires; negative once it has, `None`
    /// when the credentials file states no expiry.
    ///
    /// v0.2.95: the gateway has emitted this since v0.2.94 and the GUI has
    /// read it since v0.2.94 (`describeOAuthExpiry`, `OAUTH_WARN_SECONDS`),
    /// but this struct never carried it — so the field was dropped on the way
    /// through, `undefined` reached the card, and the "re-login within N
    /// minutes" warning could not fire on any machine. A promise with the
    /// consumer already written; the missing half was here.
    #[serde(default)]
    pub oauth_expires_in_s: Option<i64>,
    #[serde(default)]
    pub vendors: Vec<String>,
    #[serde(default)]
    pub vendor_keys_cached: Vec<String>,
    /// Which scope the gateway's vendor keys resolve in, and whether that
    /// scope resolves at all (v0.2.95, R5b).
    ///
    /// `vendors` beside an empty `vendor_keys_cached` reads like "no key
    /// configured yet". On 2026-09-10 the truth was "this daemon's working
    /// directory is not a registered project, so it cannot see ANY key you
    /// configure" — eight hours of 503s with the key present the whole time.
    /// Rendered verbatim rather than interpreted here: `resolvable` is
    /// TRI-state (`null` = nothing has probed it, which is a different claim
    /// from `false`).
    #[serde(default)]
    pub secret_scope: Option<serde_json::Value>,
    /// Where per-chat token rows land, and how many this gateway process has
    /// written (v0.2.95, the usage ledger). Counters and a path, never rows —
    /// `/health` is unauthenticated.
    #[serde(default)]
    pub usage_ledger: Option<serde_json::Value>,
    /// `owner_only` / `broader` / `unknown` for the gateway's token file.
    #[serde(default)]
    pub token_file_permissions: String,
}

// ─── Login registration: the THIRD state ──────────────────────────────────

/// What the gateway's login registration is, right now — read from the ONE
/// home, `python -m vco_lib.gateway_ensure status --json`.
///
/// The toggle used to have two positions, "enabled" and "disabled", derived
/// from the daemon's `--boot-status` exit code. That is one state short, and
/// the missing one is the state this machine sat in for eight hours on
/// 2026-09-10: REGISTERED, `enabled`, and unable to run — a unit whose
/// `ExecStart` named an interpreter that cannot import `model_router`.
/// Collapsing it into either neighbour is a lie in both directions:
/// "registered" hides that nothing can start, and "not registered" hides that
/// there IS a registration to repair.
///
/// Nothing here is re-derived from a unit file in Rust. The unit/plist/task
/// is read, and its entry point VERIFIED by running it with `--version`, in
/// `vco_lib.gateway_ensure`; this struct is a reader of that answer.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GatewayRegistration {
    /// `running` / `registered_not_running` / `registered_but_unrunnable` /
    /// `not_registered` / `start_failed` / `disabled_by_env`.
    #[serde(default)]
    pub state: String,
    /// The sentence to show. Always populated — every state is named,
    /// including the silent ones.
    #[serde(default)]
    pub reason: String,
    /// `true` / `false` / `null` (not probed). Tri-state on purpose: "I did
    /// not check" is not "it cannot run".
    #[serde(default)]
    pub runnable: Option<bool>,
    /// The artefact's path, for a card that has to say WHERE.
    #[serde(default)]
    pub unit_path: Option<String>,
}

/// What the hub's gateway supervisor last concluded, when it gave up.
///
/// Written by `vct_hub::gateway_watchdog` into launcher.db `app_state` under
/// [`HUB_GATEWAY_CONDITION_KEY`] and deleted by it the moment the gateway
/// serves again. The hub is detached and its stderr goes nowhere a user
/// looks, so without this the only evidence of "I tried three times and
/// stopped" would be a log file nobody opens.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HubGatewayCondition {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub attempts: u32,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub observed_at_ms: i64,
}

/// MUST MATCH `vct_hub::gateway_watchdog::APP_STATE_KEY_CONDITION` — pinned
/// from that side by `the_condition_key_is_the_one_the_launcher_reads`, which
/// reads THIS file.
pub const HUB_GATEWAY_CONDITION_KEY: &str = "hub.model_gateway.condition";

// ─── Status payload ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct ModelGatewayStatus {
    /// `running` / `stale_pid_file` / `not_running`.
    pub process: String,
    pub pid: Option<u32>,
    /// True when THIS launcher session started the gateway and still holds
    /// the child handle — the only case in which stopping it is safe.
    pub supervised: bool,
    /// Who would restart it if it died: `launcher` / `boot_service` /
    /// `unsupervised` / `unknown` / `not_running`. See [`supervision_word`].
    pub supervision: String,
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
    /// The login registration in full, including the third state
    /// ("registered but unrunnable"). `None` means NOT ASKED, which is the
    /// case while the gateway is answering: a serving gateway is proof enough
    /// that its registration runs, and the answer costs a subprocess plus a
    /// `--version` run of the registered argv, which does not belong on a
    /// five-second poll. `None` therefore never means "not registered" —
    /// that is `Some(state = "not_registered")`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub registration: Option<GatewayRegistration>,
    /// The hub supervisor's last verdict, when it gave up on restarting the
    /// gateway. `None` when there is nothing recorded — which is the normal
    /// state, because the row is deleted as soon as the gateway serves again.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hub_condition: Option<HubGatewayCondition>,
    /// The host token exists on disk, i.e. the gateway has run at least
    /// once. Presence only; the value is never read here.
    pub token_present: bool,
    /// The launcher can resolve an interpreter to run the daemon with.
    pub python: Option<String>,
    /// The dogfood verdict, when this payload came from a START. `None` on
    /// an ordinary status poll: the proof sends two real requests and takes
    /// seconds, so it belongs to the action a user waited for, not to a
    /// five-second refresh.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dogfood: Option<serde_json::Value>,
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

/// `python -m vco_lib.gateway_freshness` — the staleness check and the
/// restart behind the post-update modal (`commands::gateway_freshness`). Same
/// spawn recipe as the daemon, so the checkout it hashes is the checkout the
/// daemon runs from.
pub(crate) fn gateway_freshness_command(
    python: &Path,
    orchestrator_root: Option<&Path>,
) -> Command {
    python_module_command(python, "vco_lib.gateway_freshness", orchestrator_root)
}

/// `python -m vco_lib.gateway_usage` — the subscription usage windows behind
/// the home-page card (`commands::gateway_usage`). Same spawn recipe as the
/// daemon, so the port it resolves honours the same `VCT_MODEL_GATEWAY_*`
/// pins; the host token is read by that module, never by this process.
pub(crate) fn gateway_usage_command(python: &Path, orchestrator_root: Option<&Path>) -> Command {
    python_module_command(python, "vco_lib.gateway_usage", orchestrator_root)
}

/// The writer spawn. Same `PYTHONPATH` as the daemon spawn (review R1-4):
/// the writer imports `model_router` to resolve the gateway's port and its
/// context table, and answers with a DEFAULT rather than an error when it
/// cannot.
pub(crate) fn vscode_settings_command(python: &Path, orchestrator_root: Option<&Path>) -> Command {
    python_module_command(python, "vco_lib.vscode_settings", orchestrator_root)
}

/// Spawn the daemon on `port`, detached from the GUI's stdio.
///
/// ONE home for the two callers that must not drift: the user pressing Start
/// ([`model_gateway_start`]) and the supervisor restarting a child that died
/// ([`GatewaySupervisor::supervise`]). A respawn built from a second copy of
/// this command is a respawn that silently loses the port pin or the
/// PYTHONPATH the first one had.
fn spawn_gateway_child(port: u16) -> Result<Child, String> {
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_command(&python, root.as_deref());
    cmd.arg("--port").arg(port.to_string());
    // The daemon writes the port file from its own resolution, and the
    // status poll reads it back; pin the env too so a probe issued before
    // the daemon has written the file still asks the right port.
    cmd.env(PORT_ENV, port.to_string());
    // The daemon logs to its own file; a detached child must not inherit the
    // GUI's stdio.
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| {
            format!(
                "could not start the model gateway with {}: {}",
                python.display(),
                e
            )
        })
}

pub(crate) fn python_or_err() -> Result<PathBuf, String> {
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
fn run_to_completion(cmd: Command, label: &str) -> Result<(i32, String, String), String> {
    run_to_completion_within(cmd, label, PY_TIMEOUT)
}

/// [`run_to_completion`] with an explicit deadline, for the one caller whose
/// work legitimately outlasts the default: the dogfood proof sends real
/// requests to a real API (see [`DOGFOOD_TIMEOUT`]).
pub(crate) fn run_to_completion_within(
    mut cmd: Command,
    label: &str,
    limit: Duration,
) -> Result<(i32, String, String), String> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("{}: spawn failed: {}", label, e))?;

    let deadline = Instant::now() + limit;
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
                        limit.as_secs()
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

/// How long the connect half of the availability probe waits.
///
/// Loopback answers or refuses in microseconds; this bounds the pathological
/// case (a firewall blackholing 127.0.0.1) instead of stalling a click.
const PORT_CONNECT_TIMEOUT: Duration = Duration::from_millis(200);

// Test-only override for [`port_answers`]. `None` means "ask the network".
//
// It exists because NO real socket can distinguish "[`port_is_free`]
// consults the connect probe" from "[`port_is_free`] is the bind probe" on
// Linux: a live listener there fails the bind too, whatever address it is
// bound to (verified empirically for specific, wildcard, dual-stack `[::]`
// and `SO_REUSEPORT` listeners). The platform where the difference is
// observable — macOS/BSD, where `SO_REUSEADDR` lets a specific bind succeed
// under a wildcard listener — is not the one this suite runs on. Without a
// seam the composition would be two lines of wiring no test can mutate, and
// the repo has been burned before by a mechanism credited with no evidence
// it fires. The seam is in the I/O primitive, not in the decision.
#[cfg(test)]
thread_local! {
    static ANSWERS_OVERRIDE: std::cell::Cell<Option<bool>> =
        const { std::cell::Cell::new(None) };
}

/// Does a live listener ANSWER on this loopback port?
///
/// A successful `connect()` means something accepted, and it does so whether
/// that listener is bound to `127.0.0.1` specifically or to the wildcard
/// `0.0.0.0` — which is the occupant a bind probe can miss. A REFUSED
/// connection is the answer "nothing is there", not an error.
pub(crate) fn port_answers(port: u16) -> bool {
    #[cfg(test)]
    if let Some(forced) = ANSWERS_OVERRIDE.with(|c| c.get()) {
        return forced;
    }
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    match std::net::TcpStream::connect_timeout(&addr, PORT_CONNECT_TIMEOUT) {
        Ok(stream) => {
            // Closed immediately; this probe leaves nothing behind and must
            // never hold a connection open against the occupant.
            let _ = stream.shutdown(std::net::Shutdown::Both);
            true
        }
        Err(_) => false,
    }
}

/// Can a listener BIND this loopback port right now?
///
/// The question that predicts whether the daemon we are about to spawn comes
/// up, and the only one that sees a port held by something that is listening
/// but not accepting. The listener is dropped immediately, so this leaves
/// nothing behind.
pub(crate) fn port_binds(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// Is this loopback port free for a gateway to bind? The composition, with
/// both probes injected so the ORDER and the short-circuit are unit-testable
/// without occupying a real port.
pub(crate) fn port_is_free_with<A, B>(port: u16, answers: A, binds: B) -> bool
where
    A: Fn(u16) -> bool,
    B: Fn(u16) -> bool,
{
    !answers(port) && binds(port)
}

/// Is this loopback port free for a gateway to bind?
///
/// TWO questions, in this order, and both must answer yes:
///
///   1. [`port_answers`] — does a live listener accept here? A bind probe
///      alone can say "free" while one does. `std::net::TcpListener::bind`
///      sets `SO_REUSEADDR` on every non-Windows platform, and on macOS/BSD
///      that flag ALSO relaxes the wildcard-versus-specific check: a bind to
///      `127.0.0.1:P` there SUCCEEDS while another process listens on
///      `0.0.0.0:P`. The starter would then hand the daemon an address it
///      cannot serve on, and report a start that never happened.
///   2. [`port_binds`] — can a listener take it? Catches the occupant that
///      holds a port without accepting, which no connect can see.
///
/// The connect probe can only ever turn "free" into "taken", never the other
/// way round, so this is a strict tightening of the previous bind-only
/// answer — the leave-alone half (a genuinely free port stays free) is
/// unchanged.
///
/// SHARED DESIGN with the daemon's own availability rule in
/// `claude_mcp_servers/model_router/__main__.py::_bind_socket` (v0.2.94):
/// both sides answer "is this port usable" for the SAME start, so a launcher
/// that calls a port free where the daemon's bind refuses it reports a
/// gateway that is not there. MUST MATCH that function's rule, and the two
/// sides now hold the SAME socket options: `SO_REUSEADDR` on every
/// non-Windows target (Rust std sets it in `TcpListener::bind`; the daemon in
/// `_apply_reuse_flags`) and no reuse option on Windows, where the flag means
/// "steal a port another process is listening on". On EVERY OS the
/// `connect()` of question 1 is the liveness half — the half a reuse flag
/// cannot weaken, because it is not a bind — so the two "is it free?" answers
/// agree wherever the gateway runs.
pub(crate) fn port_is_free(port: u16) -> bool {
    port_is_free_with(port, port_answers, port_binds)
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
    db: State<'_, crate::db::Db>,
) -> Result<ModelGatewayStatus, String> {
    // `db` is injected by Tauri, not passed from the frontend: the JS call is
    // still `invoke('model_gateway_status')` with no arguments. It is here so
    // the card can show what the DETACHED hub supervisor concluded.
    status_on_port(&supervisor, &db, resolve_port()).await
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
    db: &crate::db::Db,
    port: u16,
) -> Result<ModelGatewayStatus, String> {
    // `supervise`, not `poll`: a child of ours that died is restarted once
    // here, because nothing else will (the boot service is opt-in and off by
    // default). See `GatewaySupervisor::supervise`.
    let supervised_pid = supervisor.supervise();
    let (state, pid) = probe_process();
    let (reachable, health, health_error) = probe_health(port).await;
    let boot = tauri::async_runtime::spawn_blocking(boot_status_word)
        .await
        .unwrap_or_else(|_| "unsupported".to_string());
    // Asked ONLY when nothing is serving — see the field's doc for why a
    // serving gateway needs no registration probe, and why `None` here is
    // "not asked" rather than "not registered".
    let registration = if reachable == Some(true) {
        None
    } else {
        tauri::async_runtime::spawn_blocking(read_registration)
            .await
            .unwrap_or(None)
    };
    let hub_condition = hub_gateway_condition(db);
    let supervised = supervised_pid.is_some() && supervised_pid == pid;
    let supervision = {
        let running = state == ProcessState::Running;
        let boot_word = boot.clone();
        tauri::async_runtime::spawn_blocking(move || {
            supervision_word(running, supervised, pid, &boot_word)
        })
        .await
        .unwrap_or_else(|_| "unknown".to_string())
    };

    Ok(ModelGatewayStatus {
        process: state.as_str().to_string(),
        pid: pid.or(supervised_pid),
        supervised,
        supervision,
        port,
        base_url: base_url(port),
        reachable,
        health,
        health_error,
        boot,
        registration,
        hub_condition,
        token_present: token_path().is_file(),
        python: resolve_python_for_vco_lib().map(|p| p.to_string_lossy().to_string()),
        dogfood: None,
    })
}

/// `--boot-status` prints one contract word and exits 0/1/2/3. Anything the
/// launcher cannot classify becomes `unsupported`, which the GUI renders as
/// a disabled toggle with a reason — never as a confident "off".
///
/// It answers the TOGGLE's question ("is a registration present?"), which is
/// binary. The third state — present but unable to run — is a different
/// question and is answered by [`read_registration`]; the two are kept apart
/// so the toggle keeps reflecting exactly what turning it on and off does.
fn boot_status_word() -> String {
    match run_gateway_cli(&["--boot-status"]) {
        Ok((0, _, _)) => "enabled".to_string(),
        Ok((1, _, _)) | Ok((2, _, _)) => "disabled".to_string(),
        _ => "unsupported".to_string(),
    }
}

/// Read the login registration from the ONE home:
/// `python -m vco_lib.gateway_ensure status --json`.
///
/// `status` STARTS NOTHING and WRITES NOTHING — that is its contract, and it
/// is what makes it safe on a GUI poll. `ensure` is the other subcommand and
/// is never called from here: the launcher's Start button is an explicit user
/// action with its own path, and the always-on restarting belongs to the hub
/// (`vct_hub::gateway_watchdog`), not to whichever GUI happens to be open.
///
/// `None` when the call could not be made or did not answer in JSON — which
/// the card renders as "could not ask", never as "not registered".
fn read_registration() -> Option<GatewayRegistration> {
    let python = resolve_python_for_vco_lib()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = python_module_command(&python, "vco_lib.gateway_ensure", root.as_deref());
    cmd.arg("status").arg("--json");
    // Exit 3 (`registered_but_unrunnable`) and 4 (`start_failed`) are
    // ANSWERS, not failures: the payload is on stdout either way, so the
    // exit code is deliberately not consulted here.
    let (_code, stdout, _stderr) = run_to_completion(cmd, "vco_lib.gateway_ensure").ok()?;
    serde_json::from_str::<GatewayRegistration>(stdout.trim()).ok()
}

/// The hub supervisor's recorded verdict, or `None`.
///
/// Read-only and soft: a missing row, a damaged row, or a DB that cannot be
/// read all mean "nothing recorded", because the alternative — failing the
/// whole status poll over a diagnostic field — would take the card down for
/// the one condition it exists to explain.
fn hub_gateway_condition(db: &crate::db::Db) -> Option<HubGatewayCondition> {
    let raw = db.app_state_get(HUB_GATEWAY_CONDITION_KEY).ok()??;
    serde_json::from_str::<HubGatewayCondition>(&raw).ok()
}

#[command]
pub async fn model_gateway_start(
    supervisor: State<'_, GatewaySupervisor>,
    db: State<'_, crate::db::Db>,
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
    let child = spawn_gateway_child(chosen)?;

    if let Ok(mut guard) = supervisor.0.lock() {
        *guard = Some(Supervised {
            child: Some(child),
            port: chosen,
            dead_since: None,
            respawns: 0,
        });
    }

    // Wait for the daemon to ANSWER, rather than sleeping and assuming
    // (review R1-1a). `/health` answering with our service name is the only
    // evidence that the process bound the port AND wrote its token and port
    // files — which is what every later resolution reads.
    poll_until(START_POLL_ATTEMPTS, START_POLL_INTERVAL, || {
        gateway_answers_on(chosen)
    })
    .await;
    // Prove it before reporting success. A gateway that ANSWERS is not a
    // gateway that answers CORRECTLY, and every defect this release fixed
    // reached a user through a start that said "started" on nothing more
    // than a 200 from `/health`. Best-effort by design: a proof that cannot
    // run (no Claude login, no network) must not turn a working start into a
    // failure, so only an explicit `refused` is carried to the GUI, and even
    // then as a WARNING beside a status the user can still act on.
    let dogfood = tauri::async_runtime::spawn_blocking(move || run_dogfood(chosen))
        .await
        .unwrap_or(None);

    // Reported on the port we CHOSE, not on whatever the port file says yet:
    // the daemon may not have written it, and a status naming the old port
    // would tell the user the start failed. When the poll timed out the
    // status carries that honestly (`reachable`/`health` from a real probe),
    // and the GUI gates its "started on port N" line on it.
    let mut status = status_on_port(&supervisor, &db, chosen).await?;
    status.dogfood = dogfood;
    Ok(status)
}

/// Run the Python dogfood proof for `port`; `None` when it could not run.
///
/// Through the SAME spawn path as every other `vco_lib` call
/// (`vscode_settings_command`), so the interpreter, the `PYTHONPATH` and the
/// state dir are the ones the rest of this module uses — a second spawn
/// recipe here would be a second set of things to keep in step.
fn run_dogfood(port: u16) -> Option<serde_json::Value> {
    // The `_or` resolver (lane D, `vct_launcher_core::python_resolve`): a
    // machine whose venv resolution fails still gets a chance at the proof
    // through whatever `python3` is on PATH, and a wrong interpreter simply
    // fails the spawn — which is reported, not treated as evidence.
    let python =
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib_or("python3");
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = vscode_settings_command(&python, root.as_deref());
    cmd.arg("dogfood").arg("--port").arg(port.to_string());
    let (_code, stdout, stderr) =
        match run_to_completion_within(cmd, "vct-dogfood", DOGFOOD_TIMEOUT) {
        Ok(out) => out,
        Err(e) => {
            tracing::warn!("[vct] model gateway: dogfood proof did not run: {}", e);
            return None;
        }
    };
    match serde_json::from_str::<serde_json::Value>(stdout.trim()) {
        Ok(v) => {
            if v.get("status").and_then(|s| s.as_str()) == Some("refused") {
                tracing::warn!(
                    "[vct] model gateway: dogfood REFUSED ({}): {}",
                    v.get("reason").and_then(|r| r.as_str()).unwrap_or("-"),
                    v.get("message").and_then(|m| m.as_str()).unwrap_or("-")
                );
            }
            Some(v)
        }
        Err(e) => {
            tracing::warn!(
                "[vct] model gateway: dogfood output was not JSON ({}): {}",
                e,
                stderr.trim()
            );
            None
        }
    }
}

/// What Stop does to the supervisor: EMPTY it, and report the live pid it
/// was holding (`None` when the child had already died).
///
/// A function rather than two lines inside the command, because the property
/// that matters is a leave-alone one and has to be testable without Tauri
/// state: `poll` alone reaps a dead child but leaves the ENTRY, so a Stop
/// pressed after a crash answered "no gateway is running" and the next status
/// poll cheerfully respawned what the user had just stopped. A stop is also a
/// cancellation of any pending respawn, whatever else it reports.
fn take_for_stop(supervisor: &GatewaySupervisor) -> (Option<Supervised>, Option<u32>) {
    let taken = supervisor.0.lock().ok().and_then(|mut g| g.take());
    let pid = taken
        .as_ref()
        .and_then(|entry| entry.child.as_ref().map(|child| child.id()));
    (taken, pid)
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
    let (taken, supervised_pid) = take_for_stop(&supervisor);
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
        let taken = taken.and_then(|entry| entry.child);
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
        // The scratch root plus the knob, set and restored by the one guard
        // (it holds the workspace-wide env mutex).
        let _g = state_dir_guard_with(&[
            (PORT_ENV, None),
            ("VCT_MODEL_GATEWAY_SECRET_PROJECT", Some("acme")),
        ]);

        let cmd = gateway_command(Path::new("/usr/bin/python3"), None);
        let forwarded = cmd.get_envs().any(|(k, v)| {
            k.to_string_lossy() == "VCT_MODEL_GATEWAY_SECRET_PROJECT"
                && v.map(|vv| vv.to_string_lossy() == "acme").unwrap_or(false)
        });

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
        let _g = state_dir_guard_with(&[(PORT_ENV, None), ("KG_COLLECTION", Some("SENTINEL"))]);

        let cmd = gateway_command(Path::new("/usr/bin/python3"), None);
        let leaked = cmd
            .get_envs()
            .any(|(k, _)| k.to_string_lossy() == "KG_COLLECTION");

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

    // ── post-update restart of a launcher-held child (v0.2.97) ──────────

    #[test]
    fn restart_of_a_held_child_leaves_everything_alone_when_nothing_is_held() {
        let sup = GatewaySupervisor::default();
        let mut spawned = false;
        let out = sup.restart_held_child_with(1234, |_| {
            spawned = true;
            Err("must not spawn".to_string())
        });
        assert_eq!(out, Ok(None));
        assert!(!spawned, "no held child means nothing is stopped or started");
    }

    /// Review R1 F9: the decision was made about one child; if the supervisor
    /// replaced it during the (up to 30 s) check, the replacement is left
    /// alone — it is already running the new code.
    #[cfg(unix)]
    #[test]
    fn restart_of_a_held_child_leaves_a_different_child_alone() {
        let sup = GatewaySupervisor::default();
        let fresh = Command::new("sleep").arg("30").spawn().expect("spawn sleep");
        let fresh_pid = fresh.id();
        *sup.0.lock().unwrap() = Some(Supervised {
            child: Some(fresh),
            port: 11498,
            dead_since: None,
            respawns: 1,
        });
        let mut spawned = false;
        let decided_pid = fresh_pid.wrapping_add(1); // the child that died
        let out = sup.restart_held_child_with(decided_pid, |_| {
            spawned = true;
            Err("must not spawn".to_string())
        });
        assert_eq!(out, Ok(None));
        assert!(!spawned);
        assert!(pid_is_alive(fresh_pid), "the fresh child must NOT be killed");
        assert_eq!(sup.held_child_pid(), Some(fresh_pid));
        let held = sup.0.lock().unwrap().take();
        if let Some(mut c) = held.and_then(|entry| entry.child) {
            let _ = c.kill();
            let _ = c.wait();
        }
    }

    #[cfg(unix)]
    #[test]
    fn restart_of_a_held_child_stops_it_and_respawns_on_the_same_port() {
        fn sleeper() -> Child {
            Command::new("sleep").arg("30").spawn().expect("spawn sleep")
        }
        let sup = GatewaySupervisor::default();
        let old = sleeper();
        let old_pid = old.id();
        *sup.0.lock().unwrap() = Some(Supervised {
            child: Some(old),
            port: 11499,
            dead_since: None,
            respawns: 0,
        });
        let mut asked_port = None;
        let out = sup
            .restart_held_child_with(old_pid, |port| {
                asked_port = Some(port);
                Ok(sleeper())
            })
            .expect("restart");
        let (new_pid, port) = out.expect("a held child was restarted");
        assert_eq!(asked_port, Some(11499), "respawned on the SAME port");
        assert_eq!(port, 11499);
        assert_ne!(new_pid, old_pid);
        assert!(!pid_is_alive(old_pid), "the old child was stopped");
        assert_eq!(sup.held_child_pid(), Some(new_pid));
        // Clean up the test's own sleeper.
        let held = sup.0.lock().unwrap().take();
        if let Some(mut c) = held.and_then(|entry| entry.child) {
            let _ = c.kill();
            let _ = c.wait();
        }
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

    /// A port nothing holds right now. Discovered by binding :0 and letting
    /// the socket go, so the tests below can bind it themselves the way they
    /// need to (specific address, or wildcard).
    fn a_free_port() -> u16 {
        let probe = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = probe.local_addr().unwrap().port();
        drop(probe);
        port
    }

    #[test]
    fn a_live_listener_on_the_specific_address_is_not_free() {
        let port = a_free_port();
        let held = std::net::TcpListener::bind(("127.0.0.1", port)).unwrap();
        assert!(port_answers(port), "a listener that accepts must answer");
        assert!(!port_is_free(port), "a live listener means the port is taken");
        drop(held);
    }

    #[test]
    fn a_live_listener_on_the_wildcard_address_is_not_free() {
        // The case the bind probe alone can miss: on macOS/BSD a bind to
        // 127.0.0.1:P succeeds while another process listens on 0.0.0.0:P,
        // because `TcpListener::bind` sets SO_REUSEADDR there. The connect
        // probe sees the occupant on every platform, which is why it runs
        // first.
        let port = a_free_port();
        let held = std::net::TcpListener::bind(("0.0.0.0", port)).unwrap();
        assert!(
            port_answers(port),
            "a wildcard listener accepts loopback connections"
        );
        assert!(
            !port_is_free(port),
            "a wildcard listener owns this port; calling it free hands the \
             daemon an address it cannot serve on"
        );
        drop(held);
    }

    #[test]
    fn a_released_port_reads_as_free_again() {
        // LEAVE-ALONE half: the connect probe tightens the answer for an
        // OCCUPIED port and must not make a free one unusable.
        let port = a_free_port();
        let held = std::net::TcpListener::bind(("0.0.0.0", port)).unwrap();
        assert!(!port_is_free(port));
        drop(held);
        assert!(!port_answers(port), "nothing accepts once the listener is gone");
        assert!(port_is_free(port), "a released port is free again");
    }

    /// Sets [`ANSWERS_OVERRIDE`] and clears it on drop, so a panicking
    /// assertion cannot leave the forced answer behind for the next test on
    /// this thread.
    struct ForcedAnswer;

    impl ForcedAnswer {
        fn yes() -> Self {
            ANSWERS_OVERRIDE.with(|c| c.set(Some(true)));
            Self
        }
    }

    impl Drop for ForcedAnswer {
        fn drop(&mut self) {
            ANSWERS_OVERRIDE.with(|c| c.set(None));
        }
    }

    #[test]
    fn port_is_free_consults_the_connect_probe_not_only_the_bind() {
        // The SHIPPED entry point, mutation-provable: reduce it to the bind
        // probe alone and this goes red. The real-socket tests above cannot
        // catch that on Linux (see `ANSWERS_OVERRIDE`), and the injected
        // composition below tests `port_is_free_with`, not the function the
        // starter actually calls.
        let port = a_free_port();
        assert!(
            port_is_free(port),
            "baseline: nothing holds this port, so both probes say free"
        );

        let _forced = ForcedAnswer::yes();
        assert!(
            !port_is_free(port),
            "something answers on this port; a successful BIND must not \
             overrule that — on macOS/BSD a bind under a wildcard listener \
             succeeds and the daemon would be handed an address it cannot \
             serve on"
        );
    }

    #[test]
    fn the_connect_probe_runs_first_and_short_circuits_the_bind() {
        // The composition itself, with both halves injected — the ORDER is
        // the whole point and a real socket cannot demonstrate it on Linux
        // (there a live listener fails the bind too, so a bind-only
        // implementation would pass the socket tests above).
        let binds_attempted = std::cell::Cell::new(0u32);
        let answered = port_is_free_with(
            12345,
            |_| true,
            |_| {
                binds_attempted.set(binds_attempted.get() + 1);
                true
            },
        );
        assert!(
            !answered,
            "something answers there; a successful bind must not overrule it"
        );
        assert_eq!(
            binds_attempted.get(),
            0,
            "the bind probe must not run once the port is known to be taken"
        );

        // ...and the bind still decides when nothing answers.
        assert!(port_is_free_with(12345, |_| false, |_| true));
        assert!(!port_is_free_with(12345, |_| false, |_| false));
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
        let pkg = repo_root.join("claude_mcp_servers").join("model_router");
        let config =
            std::fs::read_to_string(pkg.join("config.py")).expect("model_router/config.py readable");
        let server =
            std::fs::read_to_string(pkg.join("server.py")).expect("model_router/server.py readable");

        // Matched as a WHOLE LINE, never as a bare substring: `server.py`
        // still spells the name inside a help string
        // (`vct-model-gateway --print-token-path`), so a `contains` test
        // would stay green while `/health` answered with something else
        // entirely — the one failure this test exists to catch.
        //
        // CROSS-LANE (v0.2.94): the gateway declares the word once, as
        // `model_router.config.SERVICE_NAME`, and `server.py` emits
        // `"service": SERVICE_NAME`. Until that lands, the literal is still
        // inline in the health payload; both states are pinned, so this test
        // cannot quietly become vacuous at the moment of the merge.
        let declares_constant = config
            .lines()
            .any(|l| l.starts_with("SERVICE_NAME") && l.contains('='));
        if declares_constant {
            let declaration = format!("SERVICE_NAME = \"{}\"", GATEWAY_SERVICE);
            assert!(
                config.lines().any(|l| l.trim_end() == declaration),
                "model_router/config.py declares SERVICE_NAME as something \
                 other than {:?}; the starter's 'is this port MINE?' test \
                 reads a word the gateway no longer emits",
                declaration
            );
            assert!(
                server.contains("\"service\": SERVICE_NAME"),
                "config.py declares SERVICE_NAME but /health does not emit \
                 it — the constant and the answered word have drifted apart"
            );
        } else {
            assert!(
                server.contains(&format!("\"service\": \"{}\"", GATEWAY_SERVICE)),
                "the starter decides 'is this port MINE?' by a service name \
                 the gateway no longer emits"
            );
        }
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
        let _g = state_dir_guard_with(&[(PORT_ENV, Some("11437"))]);

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

    // ── v0.2.94: who is watching this process? ───────────────────────────
    //
    // The gateway that died at 04:18 was hand-started, and the card said
    // "running" until someone looked. These pin the DECISION — including the
    // two leave-alone answers, which are the ones a helpful-sounding default
    // would get wrong.

    #[test]
    fn a_hand_started_gateway_is_named_unsupervised() {
        assert_eq!(
            supervision_word(true, false, Some(4242), "disabled"),
            "unsupervised"
        );
    }

    #[test]
    fn our_own_child_is_supervised_by_the_launcher() {
        assert_eq!(
            supervision_word(true, true, Some(4242), "disabled"),
            "launcher"
        );
    }

    #[test]
    fn autostart_on_but_unverifiable_is_unknown_not_supervised() {
        // Only reachable where the unit's pid cannot be read — every
        // non-Linux host, and a Linux one without systemd. The point is that
        // it is NOT "boot_service": claiming a supervisor we could not
        // confirm is the false comfort this word exists to refuse.
        if boot_service_main_pid().is_none() {
            assert_eq!(
                supervision_word(true, false, Some(4242), "enabled"),
                "unknown"
            );
        }
    }

    #[test]
    fn a_stopped_gateway_is_not_described_as_unsupervised() {
        assert_eq!(
            supervision_word(false, false, None, "disabled"),
            "not_running"
        );
    }

    #[test]
    fn the_respawn_budget_is_one_and_the_backoff_is_real() {
        // A loop is the boot service's problem to bound, not ours: the
        // launcher restarts a child ONCE and then reports. If this ever
        // grows, it has become a second supervisor with its own rules.
        assert_eq!(MAX_RESPAWNS, 1);
        assert!(RESPAWN_BACKOFF >= Duration::from_secs(1));
    }

    #[test]
    fn an_empty_supervisor_never_spawns_anything() {
        // `supervise` may start a process, so the "nothing to do" case is
        // worth pinning: no entry means no spawn, whatever the poll cadence.
        let supervisor = GatewaySupervisor::default();
        assert!(supervisor.supervise().is_none());
        assert!(supervisor.0.lock().unwrap().is_none());
    }

    #[test]
    fn a_death_we_have_not_waited_out_is_not_respawned_yet() {
        // The backoff is measured across polls rather than slept through, so
        // the first poll after a death must decline — otherwise a start
        // failure becomes a tight restart loop driven by the GUI's timer.
        let supervisor = GatewaySupervisor::default();
        *supervisor.0.lock().unwrap() = Some(Supervised {
            child: None,
            port: 11436,
            dead_since: Some(Instant::now()),
            respawns: 0,
        });
        assert!(supervisor.supervise().is_none());
        assert_eq!(supervisor.0.lock().unwrap().as_ref().unwrap().respawns, 0);
    }

    #[test]
    fn a_stop_cancels_a_pending_respawn() {
        // The leave-alone half of the respawn decision, and the one that
        // bites. Driven through `take_for_stop` — the function
        // `model_gateway_stop` itself calls — rather than through a hand
        // rolled `take()`, which would pin the test's own copy of the
        // behaviour and stay green if the command stopped doing it.
        let supervisor = GatewaySupervisor::default();
        *supervisor.0.lock().unwrap() = Some(Supervised {
            child: None,
            port: 11436,
            // Long enough ago that `supervise` WOULD respawn it.
            dead_since: Some(Instant::now() - Duration::from_secs(60)),
            respawns: 0,
        });

        let (taken, pid) = take_for_stop(&supervisor);
        assert!(taken.is_some(), "the entry was there to take");
        assert!(pid.is_none(), "the child was already dead");
        assert!(
            supervisor.supervise().is_none(),
            "a stopped gateway must not come back on the next poll"
        );
        assert!(supervisor.0.lock().unwrap().is_none());
    }

    #[test]
    fn stopping_an_empty_supervisor_reports_nothing_and_breaks_nothing() {
        let supervisor = GatewaySupervisor::default();
        let (taken, pid) = take_for_stop(&supervisor);
        assert!(taken.is_none());
        assert!(pid.is_none());
    }

    #[test]
    fn the_dogfood_deadline_exceeds_the_proofs_own_budget() {
        // Read from the Python, not re-typed: a deadline shorter than the
        // budget kills a proof that was about to answer, and the launcher
        // then reports "could not run" for a gateway being proved.
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
        let py = std::fs::read_to_string(repo_root.join("vco_lib").join("vscode_settings.py"))
            .expect("vco_lib/vscode_settings.py readable");
        let value_of = |name: &str| -> f64 {
            let line = py
                .lines()
                .find(|l| l.starts_with(&format!("{} = ", name)))
                .unwrap_or_else(|| panic!("{} not found", name));
            line.split('=').nth(1).unwrap().trim().parse().unwrap()
        };
        let worst_case =
            value_of("DOGFOOD_TOTAL_BUDGET_S") + value_of("DOGFOOD_CALL_TIMEOUT_S");
        assert!(
            (DOGFOOD_TIMEOUT.as_secs() as f64) > worst_case,
            "DOGFOOD_TIMEOUT ({}s) must exceed the proof's own worst case ({}s)",
            DOGFOOD_TIMEOUT.as_secs(),
            worst_case
        );
    }

    #[test]
    fn the_budget_is_respected_after_a_restart() {
        let supervisor = GatewaySupervisor::default();
        *supervisor.0.lock().unwrap() = Some(Supervised {
            child: None,
            port: 11436,
            dead_since: Some(Instant::now() - Duration::from_secs(60)),
            respawns: MAX_RESPAWNS,
        });
        assert!(supervisor.supervise().is_none());
    }
}
