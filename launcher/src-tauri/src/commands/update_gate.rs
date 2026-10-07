// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Update-in-progress lockfile gate (V52-AI, v0.2.52, 2026-06-09).
//!
//! Mitigates the Windows MCP fork-bomb that hit users running
//! `update orchestrator`. User reproduction (2026-06-09):
//!
//! > "Durante l'update si sono accumulati ~97 processi python
//! > (MCP search + vct-coordination che si auto-spawnano) la prima
//! > notte, e ~77 processi node (npx @upstash/context7 +
//! > @modelcontextprotocol/* in loop) stamattina. CPU al 100% per ore.
//! > Ho dovuto killarli a mano (taskkill)."
//!
//! Root cause: during the update window, launcher restart + MCP
//! supervisor restart + Claude Code's reconnection attempts overlap.
//! On Windows mandatory file locks, every MCP-spawn-against-an-
//! updating-binary fails → Claude Code retries → respawn loop.
//!
//! Fix: a lockfile at `<vct_root_dir()>/.update-in-progress.json` that
//! the launcher writes BEFORE git pull and deletes AFTER install.py
//! succeeds. The MCP servers themselves (claude_mcp_servers/**/server.py)
//! check the lockfile at startup and exit cleanly with code 75 — every
//! respawn during the update window dies immediately, no fork-bomb.
//!
//! Mirror on the Python side: `vco_lib.update_gate`. Both sides MUST
//! agree on the schema and exit code (cross-language smoke test in
//! `tests/test_v52_ai_mcp_shim.py::ShimParityTests`).
//!
//! ## Schema
//!
//! ```json
//! {
//!   "started_at": "2026-06-09T17:30:00Z",
//!   "started_by_pid": 12345,
//!   "phase": "git_pull" | "install_py" | "binary_refresh" | "complete",
//!   "expected_completion_by": "2026-06-09T17:45:00Z"
//! }
//! ```
//!
//! ## Lifecycle
//!
//! 1. The update pipeline (`update_run::run_update`, phase 4) writes the
//!    lockfile before its git operation via
//!    [`UpdateInProgressGuard::write`].
//! 2. Throughout the update, `advance_phase` updates the `phase` field.
//! 3. On Drop (success or failure), the lockfile is deleted.
//! 4. If the launcher crashes mid-update, [`cleanup_if_stale`] runs at
//!    next boot and removes the orphaned lockfile.
//!
//! Soft-fail throughout: lockfile errors must NOT block the update
//! itself (worst case: user sees the same fork-bomb behaviour pre-fix,
//! which is no worse than today's status quo).
//!
//! Some pub items below are public API surface for future cross-crate
//! consumers (vct-hub's MCP-supervision endpoints, additional Tauri
//! commands) — silence the false-positive dead-code warnings.
#![allow(dead_code)]

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::{Deserialize, Serialize};

/// Exit code MCP servers + hooks use when declining to start because an
/// update is in progress. Distinct from 0 (normal) and 1 (error) so
/// Claude Code's logs distinguish the two states.
pub const EXIT_UPDATE_IN_PROGRESS: i32 = 75;

/// Lockfile basename under `vct_root_dir()`. Hidden by leading dot so
/// it doesn't clutter `ls ~/.vct/`.
pub const LOCKFILE_BASENAME: &str = ".update-in-progress.json";

/// Default expected update duration. Set conservatively so a slow
/// venv-rebuild on cold cache doesn't trip the stale check.
pub const DEFAULT_UPDATE_DURATION_MIN: i64 = 15;

/// Current phase of the update. Reported in the lockfile so external
/// observers (CLI status, future GUI progress indicator) can see how
/// far along we are.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    GitPull,
    InstallPy,
    BinaryRefresh,
    Complete,
}

impl Phase {
    pub fn as_str(&self) -> &'static str {
        match self {
            Phase::GitPull => "git_pull",
            Phase::InstallPy => "install_py",
            Phase::BinaryRefresh => "binary_refresh",
            Phase::Complete => "complete",
        }
    }
}

/// On-disk representation of the lockfile. Schema MUST match
/// `vco_lib.update_gate` byte-for-byte (cross-language parity test).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockfilePayload {
    pub started_at: String,
    pub started_by_pid: u32,
    pub phase: Phase,
    pub expected_completion_by: String,
}

/// Return the absolute lockfile path. Honours `VCT_STATE_DIR` via
/// `vct_launcher_core::paths::vct_root_dir()`.
pub fn lockfile_path() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join(LOCKFILE_BASENAME)
}

fn iso_now() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn parse_iso(s: &str) -> Option<DateTime<Utc>> {
    if s.is_empty() {
        return None;
    }
    // Accept both `...Z` (what we write) and `...+00:00`.
    let normalised = if let Some(stripped) = s.strip_suffix('Z') {
        format!("{stripped}+00:00")
    } else {
        s.to_string()
    };
    DateTime::parse_from_rfc3339(&normalised)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
}

/// Write or overwrite the lockfile at the given path.
///
/// `expected_duration_min` is added to `now()` to produce
/// `expected_completion_by` — the stale-detection deadline.
///
/// Soft-fail: any I/O error is logged and bubbled up. The launcher's
/// update flow treats write failure as a non-fatal warning (continuing
/// without the gate re-exposes the fork-bomb, but doesn't break the
/// update itself).
pub fn write_lockfile_at(
    path: &Path,
    phase: Phase,
    expected_duration_min: i64,
) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all({}) failed: {}", parent.display(), e))?;
    }
    let expected_completion = (Utc::now() + ChronoDuration::minutes(expected_duration_min))
        .format("%Y-%m-%dT%H:%M:%SZ")
        .to_string();
    let payload = LockfilePayload {
        started_at: iso_now(),
        started_by_pid: std::process::id(),
        phase,
        expected_completion_by: expected_completion,
    };
    let json = serde_json::to_string_pretty(&payload)
        .map_err(|e| format!("serialise lockfile: {}", e))?;
    // Atomic write: temp file then rename.
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = fs::File::create(&tmp)
            .map_err(|e| format!("create {}: {}", tmp.display(), e))?;
        f.write_all(json.as_bytes())
            .map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        f.sync_all().ok();
    }
    fs::rename(&tmp, path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

/// Write/overwrite the lockfile at the canonical path.
pub fn write_lockfile(phase: Phase, expected_duration_min: i64) -> Result<(), String> {
    write_lockfile_at(&lockfile_path(), phase, expected_duration_min)
}

/// Read the lockfile, returning `None` for any failure (missing,
/// corrupt, unreadable). Callers treat `None` as "no lockfile".
pub fn read_lockfile_at(path: &Path) -> Option<LockfilePayload> {
    if !path.exists() {
        return None;
    }
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn read_lockfile() -> Option<LockfilePayload> {
    read_lockfile_at(&lockfile_path())
}

/// Delete the lockfile if present. Returns `true` if the file does not
/// exist after the call (either it never existed or removal succeeded).
pub fn delete_lockfile_at(path: &Path) -> bool {
    match fs::remove_file(path) {
        Ok(()) => true,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => true,
        Err(e) => {
            tracing::warn!(
                "[update_gate] failed to remove {}: {}",
                path.display(),
                e
            );
            false
        }
    }
}

pub fn delete_lockfile() -> bool {
    delete_lockfile_at(&lockfile_path())
}

/// Is an orchestrator update currently in progress?
///
/// Returns `true` iff the lockfile exists AND its
/// `expected_completion_by` is in the future. A lockfile whose deadline
/// has passed is treated as stale (returns `false`) — callers can
/// invoke [`cleanup_if_stale_at`] to remove it.
pub fn is_update_in_progress_at(path: &Path) -> bool {
    let Some(data) = read_lockfile_at(path) else {
        return false;
    };
    let Some(deadline) = parse_iso(&data.expected_completion_by) else {
        return false;
    };
    Utc::now() < deadline
}

pub fn is_update_in_progress() -> bool {
    is_update_in_progress_at(&lockfile_path())
}

/// v0.2.60: poller stand-down gate. Returns `true` (and logs once) when a
/// background task that opens its OWN `launcher.db` connection should SKIP
/// this tick because an orchestrator update is in progress.
///
/// WHY: the update pipeline (`update_run::run_update`) closes the launcher's managed `Db`
/// connection for the `install.py --update` window so install.py can take
/// the SQLite writer lock (Windows holds it exclusively — see the
/// launcher-self-db-lock bug). But several background pollers open their
/// OWN fresh `rusqlite::Connection::open(db_path())` (deliberately — see
/// `module_updates.rs::spawn_module_update_check_loop`'s "the main `Db`
/// State holds the only long-lived connection" note) and WRITE on timers.
/// Those connections bypass the managed-connection close entirely, so
/// without this gate a poller firing mid-update re-contends with
/// install.py and re-creates the lock-timeout → deferral → half-install
/// loop. EVERY fresh-conn poller MUST call this at the top of its tick and
/// skip when it returns true. Reuses the existing `.update-in-progress`
/// lockfile (armed by `UpdateInProgressGuard` at the start of the update)
/// — no new state, and it inherits the stale-deadline self-healing.
pub fn skip_if_update_in_progress(task_name: &str) -> bool {
    skip_if_update_in_progress_at(task_name, &lockfile_path())
}

/// Path-injectable core of [`skip_if_update_in_progress`] for unit tests
/// (mirrors the `_at` pattern used throughout this module so tests never
/// mutate the process-wide `VCT_STATE_DIR`).
pub fn skip_if_update_in_progress_at(task_name: &str, path: &Path) -> bool {
    if is_update_in_progress_at(path) {
        tracing::warn!(
            "[update_gate] {}: orchestrator update in progress — skipping this tick \
             (avoids contending with install.py for the launcher.db writer lock)",
            task_name
        );
        true
    } else {
        false
    }
}

/// Boot-time self-healing: remove a stale lockfile (deadline passed).
/// Returns `true` if a stale lockfile was found and removed.
pub fn cleanup_if_stale_at(path: &Path) -> bool {
    let Some(data) = read_lockfile_at(path) else {
        return false;
    };
    let stale = match parse_iso(&data.expected_completion_by) {
        Some(deadline) => Utc::now() >= deadline,
        None => true, // malformed deadline = stale
    };
    if stale {
        tracing::warn!(
            "[update_gate] removing stale lockfile (deadline={})",
            data.expected_completion_by
        );
        return delete_lockfile_at(path);
    }
    false
}

pub fn cleanup_if_stale() -> bool {
    cleanup_if_stale_at(&lockfile_path())
}

/// RAII guard: writes the lockfile on `new`, deletes it on `drop`.
///
/// Designed to be held for the whole orchestrator update (phases 4-10 of
/// `update_run::run_update`). Any
/// exit path (early return, panic, ?-bail) cleans up the lockfile so
/// MCPs can be respawned on the next session.
///
/// Use [`advance_phase`] on `&mut self` as the update progresses so the
/// `phase` field is current. Failures to advance are non-fatal — the
/// guard still cleans up on drop.
pub struct UpdateInProgressGuard {
    path: PathBuf,
    /// `true` once the lockfile has been successfully written. If
    /// initial write failed, we don't try to delete on drop.
    armed: bool,
}

impl UpdateInProgressGuard {
    /// Create a new guard and write the initial lockfile (phase=git_pull).
    ///
    /// Soft-fail on write error: the guard is constructed but `armed=false`,
    /// so it won't try to delete a file it didn't create. The caller
    /// receives an error string but is free to proceed with the update.
    pub fn new() -> (Self, Result<(), String>) {
        Self::new_at(lockfile_path(), DEFAULT_UPDATE_DURATION_MIN)
    }

    pub fn new_at(path: PathBuf, expected_duration_min: i64) -> (Self, Result<(), String>) {
        let write_result = write_lockfile_at(&path, Phase::GitPull, expected_duration_min);
        let armed = write_result.is_ok();
        (Self { path, armed }, write_result)
    }

    /// Advance the lockfile's `phase` field. Best-effort: any I/O error
    /// is logged but doesn't change the guard's armed state.
    pub fn advance_phase(&mut self, phase: Phase) {
        if !self.armed {
            return;
        }
        if let Err(e) = write_lockfile_at(&self.path, phase, DEFAULT_UPDATE_DURATION_MIN) {
            tracing::warn!("[update_gate] advance_phase({:?}) failed: {}", phase, e);
        }
    }

    /// Explicit cleanup helper — sometimes the caller wants to remove
    /// the lockfile early (e.g. update completed but we want to keep
    /// the guard around for borrow-checker reasons).
    pub fn disarm_and_cleanup(&mut self) {
        if self.armed {
            delete_lockfile_at(&self.path);
            self.armed = false;
        }
    }
}

impl Drop for UpdateInProgressGuard {
    fn drop(&mut self) {
        if self.armed {
            delete_lockfile_at(&self.path);
        }
    }
}

// ─── Process-table refresh contract (one home) ───────────────────────────
//
// Every process-identity sweep in this file (the pre-update MCP kill sweep,
// the steady-state orphan reaper, the hub backstop sweep, the exe-by-pid
// lookup) reads one shared snapshot shape, built by ONE helper:
// [`refresh_with_cmdlines`].
//
// WHY one home (v0.2.102, S16-1): in sysinfo 0.38 `System::refresh_processes`
// refreshes memory / cpu / disk usage / exe / tasks ONLY — it never reads a
// process's command line. On a fresh `System::new()` every `Process::cmd()`
// slice is therefore EMPTY, so a sweep that refreshed that way and then
// matched on the joined cmdline matched NOTHING. That is exactly how the MCP
// kill sweep and the orphan reaper shipped: silent no-ops (no "kill-sweep:
// terminating" line on a live update, MCPs days old surviving it) while their
// pure-helper unit tests stayed green. Cmdlines are only populated when the
// refresh asks for them with `ProcessRefreshKind::with_cmd`; this helper is
// the one place that does it, and the real-process tests below pin it.

/// Refresh `sys` with every process attribute the sweeps in this file read.
///
/// Read by the callers: pid, `cmd()` (needs `with_cmd`), `exe()` (needs
/// `with_exe`), `parent()`, `run_time()` and `name()` (the last three are
/// populated on every refresh, whatever the kind — sysinfo reads them from
/// the same stat record that identifies the process). Tasks (Linux threads,
/// which sysinfo otherwise lists as processes of their own with the owning
/// process's cmdline) are deliberately NOT refreshed: a sweep must count and
/// signal processes, not every thread of each.
pub(crate) fn refresh_with_cmdlines(sys: &mut sysinfo::System) {
    use sysinfo::{ProcessRefreshKind, ProcessesToUpdate, UpdateKind};
    sys.refresh_processes_specifics(
        ProcessesToUpdate::All,
        true,
        ProcessRefreshKind::nothing()
            .with_cmd(UpdateKind::Always)
            .with_exe(UpdateKind::OnlyIfNotSet)
            .without_tasks(),
    );
}

/// A fresh process-table snapshot built through [`refresh_with_cmdlines`].
fn process_table_with_cmdlines() -> sysinfo::System {
    let mut sys = sysinfo::System::new();
    refresh_with_cmdlines(&mut sys);
    sys
}

/// A process's argv joined with single spaces (lossy for non-UTF-8 args).
/// Empty when the snapshot was refreshed without `with_cmd`, or when the OS
/// withholds the cmdline (kernel threads, another user's process on some
/// platforms).
fn joined_cmdline(proc: &sysinfo::Process) -> String {
    proc.cmd()
        .iter()
        .map(|s| s.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(" ")
}

/// How much of the process table a sweep actually saw. Logged by every
/// cmdline-matching sweep so a regression of the refresh contract reads as
/// "enumerated 312, with a cmdline 0" in the log instead of silence.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct EnumerationStats {
    /// Processes in the snapshot.
    pub enumerated: usize,
    /// Of those, how many carried a non-empty cmdline.
    pub with_cmdline: usize,
}

fn enumeration_stats(sys: &sysinfo::System) -> EnumerationStats {
    let mut stats = EnumerationStats::default();
    for proc in sys.processes().values() {
        stats.enumerated += 1;
        if !proc.cmd().is_empty() {
            stats.with_cmdline += 1;
        }
    }
    stats
}

/// Log the enumeration counters for `sweep`; WARN when the table is non-empty
/// but no process carried a cmdline (the refresh contract has regressed and
/// the MCP filter is blind).
fn log_enumeration(sweep: &str, stats: EnumerationStats, matched: usize) {
    tracing::info!(
        "[update_gate] {}: enumerated {} process(es), {} with a cmdline, {} matched",
        sweep,
        stats.enumerated,
        stats.with_cmdline,
        matched
    );
    if stats.enumerated > 0 && stats.with_cmdline == 0 {
        tracing::warn!(
            "[update_gate] {}: no enumerated process carried a cmdline — the \
             process table was refreshed without `with_cmd`, so the MCP \
             filter cannot see anything (see refresh_with_cmdlines)",
            sweep
        );
    }
}

/// Terminate ONE process: SIGTERM (graceful) on POSIX; on Windows, which has
/// no SIGTERM (sysinfo 0.38's `kill_with(Term)` returns `None` there), the
/// fallback `kill()` runs `taskkill /PID <pid> /F` — a forced kill of that
/// process only. Used by the hub backstop sweep, where a tree kill is NOT
/// wanted. Best-effort: never escalates to SIGKILL on POSIX, never waits.
fn terminate_process(proc: &sysinfo::Process) {
    if proc.kill_with(sysinfo::Signal::Term).is_none() {
        proc.kill();
    }
}

/// Terminate an MCP server process — the terminate step of the pre-update
/// kill sweep and of the orphan reaper.
///
/// V52-AI's purpose is to free the files an MCP holds open and to stop a
/// respawn storm, so on each OS this takes down what actually holds them:
/// - **POSIX**: SIGTERM to the matched process (as [`terminate_process`]),
///   no tree walk. What happens to a matched host's children:
///   * `npm exec` / `npx` forward SIGINT/SIGTERM to the child they spawn
///     (npm's `@npmcli/run-script/lib/signal-manager.js`, read in the npm 10
///     that ships with node 20). That child is `sh -c <bin>`, which does NOT
///     forward — but the node process under it is itself an MCP server argv
///     ([`is_mcp_server_argv`] matches `node …/.bin/playwright-mcp`), so the
///     sweep signals it directly.
///   * The python wrapper MCPs (`-m claude_mcp_servers.wrappers.*`) install a
///     SIGTERM handler that terminates and reaps the upstream they host
///     (`claude_mcp_servers/wrappers/_base.py`, `WrapperMCP.run`), so e.g. the
///     excalidraw node server — not itself matched — goes down with them.
/// - **Windows**: `taskkill /PID <pid> /T /F` — the process AND its child
///   tree, forced. Windows has no SIGTERM to forward, so killing only the
///   `npm exec` / `cmd /c` host leaves its node child running and holding the
///   very file locks the sweep exists to release. Falls back to the
///   single-process kill when `taskkill` cannot be run. MCP servers do not
///   spawn the hub (they only resolve its port), so the tree is the MCP's
///   own helpers.
fn terminate_mcp_server(proc: &sysinfo::Process) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let tree_killed = std::process::Command::new("taskkill")
            .args(["/PID", &proc.pid().as_u32().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !tree_killed {
            proc.kill();
        }
    }
    #[cfg(not(windows))]
    {
        terminate_process(proc);
    }
}

// ─── Pre-update MCP kill sweep ───────────────────────────────────────────
//
// Before the launcher writes the lockfile + starts git pull, do a
// best-effort sweep of currently-running MCP processes that could be
// holding file locks on the binaries we're about to overwrite. This
// is the *prevention* side of the gate; the lockfile itself is the
// *suppression* side.
//
// CRITICAL: this sweep MUST NOT fire outside an orchestrator update
// (`update_run::run_update`). We
// don't want to kill the user's other Python or Node processes — nor an
// editor, `tail`, `grep` or shell that merely CITES an MCP path. The filter
// is [`is_mcp_server_argv`]: the process must be EXECUTING an MCP server
// (a runtime running a `claude_mcp_servers` script/module, or an npm MCP
// package), never just mentioning one.
//
// Enumeration is the shared snapshot from `refresh_with_cmdlines` (the
// filter needs argv, which only that refresh populates) on every OS;
// termination is `terminate_mcp_server` (SIGTERM on POSIX, a `taskkill /T /F`
// tree kill on Windows). Either way, failure is soft — logged but doesn't
// block the update (the lockfile + MCP-side gate catches whatever the sweep
// missed).

/// One process the MCP kill sweep would terminate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SweepCandidate {
    pub pid: u32,
    pub cmdline: String,
}

/// A process's argv as owned strings (lossy for non-UTF-8 args).
fn argv_of(proc: &sysinfo::Process) -> Vec<String> {
    proc.cmd()
        .iter()
        .map(|s| s.to_string_lossy().into_owned())
        .collect()
}

/// The ONE candidate decision of the pre-update MCP kill sweep, for one row
/// of the snapshot: an MCP server argv, never `my_pid`.
fn mcp_sweep_candidate(
    pid: sysinfo::Pid,
    proc: &sysinfo::Process,
    my_pid: u32,
) -> Option<SweepCandidate> {
    if pid.as_u32() == my_pid || !is_mcp_server_argv(&argv_of(proc)) {
        return None;
    }
    Some(SweepCandidate {
        pid: pid.as_u32(),
        cmdline: joined_cmdline(proc),
    })
}

/// The candidate filter of the pre-update MCP kill sweep over a snapshot.
/// Pure over the snapshot — it decides, it does not signal.
pub(crate) fn mcp_kill_sweep_candidates(sys: &sysinfo::System, my_pid: u32) -> Vec<SweepCandidate> {
    sys.processes()
        .iter()
        .filter_map(|(pid, proc)| mcp_sweep_candidate(*pid, proc, my_pid))
        .collect()
}

/// The MCP kill sweep over an existing snapshot, with the terminate step
/// injected (production passes [`terminate_mcp_server`]; tests pass a closure
/// that only ever terminates the child they spawned). Returns the number of
/// candidates handed to `terminate`.
fn run_mcp_kill_sweep(
    sys: &sysinfo::System,
    my_pid: u32,
    terminate: &mut dyn FnMut(&sysinfo::Process),
) -> usize {
    let hits: Vec<(&sysinfo::Process, SweepCandidate)> = sys
        .processes()
        .iter()
        .filter_map(|(pid, proc)| mcp_sweep_candidate(*pid, proc, my_pid).map(|c| (proc, c)))
        .collect();
    log_enumeration("kill-sweep", enumeration_stats(sys), hits.len());
    for (proc, cand) in &hits {
        tracing::info!(
            "[update_gate] kill-sweep: terminating PID {} (cmd: {})",
            cand.pid,
            cand.cmdline.chars().take(120).collect::<String>()
        );
        // No wait for exit in this best-effort sweep — the lockfile gate
        // prevents respawns.
        terminate(proc);
    }
    hits.len()
}

/// Run a pre-update MCP kill sweep. Best-effort, soft-fail.
///
/// Returns the number of processes that were targeted (informational
/// only — even if 0, the gate still protects future respawns).
///
/// Refuses under a cargo test binary: the sweep matches the argv of EVERY
/// process on the machine, so a test reaching it would terminate the
/// developer's live MCP servers. Tests drive [`run_mcp_kill_sweep`] with an
/// injected terminate step instead.
pub fn pre_update_mcp_kill_sweep() -> usize {
    if crate::hub_launcher::running_under_test_harness() {
        tracing::warn!(
            "[update_gate] kill-sweep: refusing to terminate MCP processes — \
             this process is a cargo test binary and the sweep would reach the \
             developer's live MCP servers"
        );
        return 0;
    }
    let sys = process_table_with_cmdlines();
    run_mcp_kill_sweep(&sys, std::process::id(), &mut terminate_mcp_server)
}

// ─── "Is this process EXECUTING an MCP server?" ──────────────────────────
//
// v0.2.102: the filter used to be "the joined cmdline CONTAINS one of
// `claude_mcp_servers` / `@modelcontextprotocol` / `@upstash/context7`". While
// the sweep was blind that was harmless; once it worked, it would SIGTERM a
// `vim …/claude_mcp_servers/x.py`, a `tail -f …/claude_mcp_servers/…log`, a
// `grep -r claude_mcp_servers`, or a `bash -c` whose text cites the path. The
// predicate now reads argv structurally: argv[0] must be a runtime, and the
// thing that runtime EXECUTES must be an MCP server.

/// npm package names (version suffix stripped) that are MCP servers. The
/// two original V52-AI entries plus `@playwright/mcp`, which VCO registers
/// itself (`npx -y @playwright/mcp@latest`, `mcp_registration.rs`).
fn is_mcp_npm_package(spec: &str) -> bool {
    let name = npm_package_name(spec);
    name.starts_with("@modelcontextprotocol/")
        || name.starts_with("@upstash/context7")
        || name == "@playwright/mcp"
}

/// Executable names those packages install under `node_modules/.bin`
/// (what `npx` / `npm exec` actually runs).
fn is_mcp_npm_bin(name: &str) -> bool {
    matches!(name, "playwright-mcp" | "context7-mcp") || name.starts_with("mcp-server-")
}

/// `@scope/name@1.2` → `@scope/name`; `name@latest` → `name`.
fn npm_package_name(spec: &str) -> &str {
    let search_from = usize::from(spec.starts_with('@'));
    match spec[search_from..].find('@') {
        Some(i) => &spec[..search_from + i],
        None => spec,
    }
}

/// Path components, split on both separators (a Windows argv uses `\`).
fn path_components(path: &str) -> Vec<&str> {
    path.split(['/', '\\']).filter(|c| !c.is_empty()).collect()
}

/// Lowercased basename with a Windows executable suffix removed:
/// `C:\Python312\python.exe` → `python`, `/usr/bin/npx` → `npx`.
fn exe_stem(path: &str) -> String {
    let base = path_components(path).last().copied().unwrap_or("").to_ascii_lowercase();
    for suffix in [".exe", ".cmd", ".bat"] {
        if let Some(stem) = base.strip_suffix(suffix) {
            return stem.to_string();
        }
    }
    base
}

/// A script path inside a `claude_mcp_servers` directory (the directory is a
/// component, not the last one).
fn is_claude_mcp_script_path(path: &str) -> bool {
    let comps = path_components(path);
    comps.len() >= 2 && comps[..comps.len() - 1].contains(&"claude_mcp_servers")
}

/// `claude_mcp_servers` or `claude_mcp_servers.<…>` as a `python -m` module.
fn is_claude_mcp_module(module: &str) -> bool {
    module == "claude_mcp_servers" || module.starts_with("claude_mcp_servers.")
}

/// A node script that is an MCP server: an npm MCP package's file or bin,
/// or a script under `claude_mcp_servers`.
fn is_mcp_node_script(path: &str) -> bool {
    let comps = path_components(path);
    let stem = |c: &str| {
        let c = c.to_ascii_lowercase();
        ["js", "cjs", "mjs"]
            .iter()
            .find_map(|ext| c.strip_suffix(&format!(".{ext}")).map(str::to_string))
            .unwrap_or(c)
    };
    comps.iter().enumerate().any(|(i, c)| {
        let next = comps.get(i + 1).copied().unwrap_or("");
        *c == "@modelcontextprotocol"
            || (*c == "@upstash" && next.starts_with("context7"))
            || (*c == "@playwright" && next == "mcp")
            || (*c == ".bin" && i + 2 == comps.len() && is_mcp_npm_bin(&stem(next)))
    }) || is_claude_mcp_script_path(path)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Runtime {
    Python,
    Node,
    Npx,
    Npm,
    Uv,
    Shell,
    PowerShell,
}

fn runtime_of(argv0: &str) -> Option<Runtime> {
    let stem = exe_stem(argv0);
    let python_versioned = stem
        .strip_prefix("python")
        .map(|rest| {
            let rest = rest.strip_suffix('w').unwrap_or(rest);
            rest.chars().all(|ch| ch.is_ascii_digit() || ch == '.')
        })
        .unwrap_or(false);
    Some(match stem.as_str() {
        _ if python_versioned => Runtime::Python,
        "py" => Runtime::Python,
        "node" | "nodejs" => Runtime::Node,
        "npx" => Runtime::Npx,
        "npm" => Runtime::Npm,
        "uv" => Runtime::Uv,
        "sh" | "bash" | "dash" | "zsh" => Runtime::Shell,
        "pwsh" | "powershell" => Runtime::PowerShell,
        _ => return None,
    })
}

/// Python: `-m claude_mcp_servers.…` or a script under `claude_mcp_servers`.
/// `-c <code>` executes text, not a server file → never a match.
fn python_executes_mcp(args: &[&str]) -> bool {
    let mut i = 0;
    while i < args.len() {
        let a = args[i];
        if a == "--" {
            return args.get(i + 1).is_some_and(|s| is_claude_mcp_script_path(s));
        }
        if a == "-" {
            return false; // script from stdin
        }
        if let Some(flags) = a.strip_prefix('-').filter(|_| !a.starts_with("--")) {
            for (pos, ch) in flags.char_indices() {
                let rest = &flags[pos + ch.len_utf8()..];
                match ch {
                    'm' => {
                        let module = if rest.is_empty() { args.get(i + 1).copied() } else { Some(rest) };
                        return module.is_some_and(is_claude_mcp_module);
                    }
                    'c' => return false,
                    'W' | 'X' => {
                        if rest.is_empty() {
                            i += 1; // the option's value
                        }
                        break;
                    }
                    _ => {}
                }
            }
            i += 1;
            continue;
        }
        if a.starts_with("--") {
            i += 1; // long options (`--check-hash-based-pycs` takes a value; harmless to over-skip a flag)
            continue;
        }
        return is_claude_mcp_script_path(a);
    }
    false
}

/// npx / `npm exec`: the command that actually RUNS — the first positional
/// (or the token after `--`) — must be an MCP package or bin. A `-p` /
/// `--package` value only installs a package for that command, so
/// `npx --package=@playwright/mcp eslint .` runs eslint and is NOT a match,
/// while `npx --package=@playwright/mcp playwright-mcp` is. `-c <shell text>`
/// executes text → never a match.
fn npx_executes_mcp(args: &[&str]) -> bool {
    let mut i = 0;
    while i < args.len() {
        let a = args[i];
        if a == "--" {
            return args.get(i + 1).is_some_and(|s| is_mcp_npm_package(s) || is_mcp_npm_bin(s));
        }
        if a == "-c" || a == "--call" || a.starts_with("--call=") {
            return false;
        }
        if a == "-p" || a == "--package" {
            i += 2; // the value names a package to install, not the command run
            continue;
        }
        if a.starts_with('-') {
            i += 1;
            continue;
        }
        return is_mcp_npm_package(a) || is_mcp_npm_bin(a);
    }
    false
}

/// Node: `-e/-p` evaluate text (never a match); otherwise the first non-option
/// token is the script. npm's own CLIs (`npx-cli.js` / `npm-cli.js`, how
/// Windows runs `npx`) hand the rest to the npx / npm rules.
fn node_executes_mcp(args: &[&str], depth: u8) -> bool {
    let mut i = 0;
    while i < args.len() {
        let a = args[i];
        match a {
            "-e" | "--eval" | "-p" | "--print" => return false,
            "-r" | "--require" | "--import" | "--loader" | "--experimental-loader" => {
                i += 2;
                continue;
            }
            _ if a.starts_with('-') => {
                i += 1;
                continue;
            }
            _ => {}
        }
        let rest = &args[i + 1..];
        return match exe_stem(a).as_str() {
            "npx-cli.js" => npx_executes_mcp(rest),
            "npm-cli.js" => npm_executes_mcp(rest, depth),
            _ => is_mcp_node_script(a),
        };
    }
    false
}

/// npm: only `npm exec` / `npm x` executes a package (`npm install
/// @modelcontextprotocol/x` and `npm list …` are left alone).
fn npm_executes_mcp(args: &[&str], _depth: u8) -> bool {
    let Some(sub) = args.iter().position(|a| !a.starts_with('-')) else {
        return false;
    };
    matches!(args[sub], "exec" | "x") && npx_executes_mcp(&args[sub + 1..])
}

/// uv: `uv run [opts] <command…>` — the command is judged like a top-level
/// argv; a bare `.py` script is run by Python.
fn uv_executes_mcp(args: &[&str], depth: u8) -> bool {
    const VALUED: &[&str] = &[
        "--with", "--python", "-p", "--project", "--directory", "--from", "--env-file",
        "--extra", "--group", "--index", "--with-requirements",
    ];
    let Some(run) = args.iter().position(|a| !a.starts_with('-')) else {
        return false;
    };
    if args[run] != "run" {
        return false;
    }
    let mut i = run + 1;
    while i < args.len() {
        let a = args[i];
        if a == "--" {
            i += 1;
            break;
        }
        if VALUED.contains(&a) {
            i += 2;
            continue;
        }
        if a.starts_with('-') {
            i += 1;
            continue;
        }
        break;
    }
    let rest = &args[i.min(args.len())..];
    match rest.first() {
        Some(first) if first.ends_with(".py") => is_claude_mcp_script_path(first),
        Some(_) => argv_executes_mcp(rest, depth + 1),
        None => false,
    }
}

/// sh/bash/dash/zsh running a script FILE under `claude_mcp_servers` (a
/// shebang wrapper). Any `-c` (incl. `-lc`, `-ec`) executes text → no match.
fn shell_executes_mcp(args: &[&str]) -> bool {
    let mut i = 0;
    while i < args.len() {
        let a = args[i];
        if a == "--" {
            return args.get(i + 1).is_some_and(|s| is_claude_mcp_script_path(s));
        }
        if (a.starts_with('-') || a.starts_with('+')) && a.len() > 1 {
            if !a.starts_with("--") && a[1..].contains('c') {
                return false;
            }
            i += if a == "-o" || a == "+o" { 2 } else { 1 };
            continue;
        }
        return is_claude_mcp_script_path(a);
    }
    false
}

/// pwsh/powershell `-File <script>` under `claude_mcp_servers`; `-Command`
/// executes text → no match.
fn powershell_executes_mcp(args: &[&str]) -> bool {
    let lower: Vec<String> = args.iter().map(|a| a.to_ascii_lowercase()).collect();
    for (i, a) in lower.iter().enumerate() {
        match a.as_str() {
            "-command" | "-c" | "-encodedcommand" | "-e" | "-ec" => return false,
            "-file" | "-f" => return args.get(i + 1).is_some_and(|s| is_claude_mcp_script_path(s)),
            _ => {}
        }
    }
    false
}

fn argv_executes_mcp(argv: &[&str], depth: u8) -> bool {
    if depth > 2 {
        return false;
    }
    let Some((argv0, args)) = argv.split_first() else {
        return false;
    };
    match runtime_of(argv0) {
        Some(Runtime::Python) => python_executes_mcp(args),
        Some(Runtime::Node) => node_executes_mcp(args, depth),
        Some(Runtime::Npx) => npx_executes_mcp(args),
        Some(Runtime::Npm) => npm_executes_mcp(args, depth),
        Some(Runtime::Uv) => uv_executes_mcp(args, depth),
        Some(Runtime::Shell) => shell_executes_mcp(args),
        Some(Runtime::PowerShell) => powershell_executes_mcp(args),
        None => false,
    }
}

/// Is this argv a process EXECUTING an MCP server? The ONE filter of the
/// pre-update kill sweep and the orphan reaper.
///
/// True only when argv[0] is a runtime (python*/py, node, npx, npm, uv, a
/// POSIX shell, pwsh) AND what it executes is an MCP server: a script under a
/// `claude_mcp_servers` directory, `-m claude_mcp_servers[.…]`, or an npm MCP
/// package / its bin (`@modelcontextprotocol/*`, `@upstash/context7*`,
/// `@playwright/mcp`). A process that only CITES such a path — an editor,
/// `tail`, `grep`, `code`, `python -c`/`bash -c` text, `npm install` — is
/// never a match. Pure, table-tested; the enumeration side is pinned by the
/// real-process tests.
///
/// Out of scope, as before this change: the excalidraw MCP's node server
/// (`node …/vco_lib/excalidraw_mcp_fork/dist/mcp/index.bundled.js`) — its
/// `-m claude_mcp_servers.wrappers.excalidraw_proxy` parent IS matched.
pub fn is_mcp_server_argv<S: AsRef<str>>(argv: &[S]) -> bool {
    let argv: Vec<&str> = argv.iter().map(AsRef::as_ref).collect();
    argv_executes_mcp(&argv, 0)
}

/// Minimum age (seconds) an MCP process must have before the steady-state
/// reaper will consider it for reaping. Guards against a race where an MCP is
/// caught in the window between fork and the harness establishing the parent
/// link — a just-spawned process can momentarily look parent-less. The
/// coordination-MCP zombie this backstops accumulates over MINUTES-to-HOURS,
/// so a conservative 60s floor costs nothing and removes the race entirely.
pub const STEADY_STATE_MCP_MIN_AGE_SECS: u64 = 60;

/// Pure decision: should the steady-state reaper reap this MCP process?
///
/// This is the conservative "is it orphaned?" predicate, isolated from process
/// enumeration so both the act AND the leave-alone case are unit-testable (per
/// the orchestrator's "test the decision that gates a destructive action" rule).
///
/// We reap ONLY when we can POSITIVELY confirm the process is orphaned. After
/// the MCP-server ([`is_mcp_server_argv`]) + age gates, EITHER orphan signal
/// qualifies:
///   (a) its known parent pid is now DEAD (`parent_alive == Some(false)`), OR
///   (b) it has been REPARENTED to a subreaper (`parent_is_subreaper == true`).
///
/// WHY (b) — the POSIX no-op fix (v0.2.71 review): on Linux/macOS the kernel
/// REPARENTS an orphan to a live subreaper (PID 1 / `systemd --user`) the
/// instant its real parent exits, so signal (a) `parent_alive == false` is
/// essentially unreachable there — the orphan always shows a LIVE parent (the
/// subreaper). Without (b) the reaper had teeth only on Windows (which does not
/// reparent). Signal (b) catches the reparented POSIX orphan: a HEALTHY MCP's
/// parent is its live Claude-session process, NEVER a subreaper, so
/// `parent_is_subreaper` distinguishes "orphaned + reparented" from
/// "session still attached" without risk to a live-session MCP. The caller
/// computes `parent_is_subreaper` (e.g. `parent_pid == 1`, or the parent's
/// comm/exe is a known subreaper like `systemd`); on Windows it is always false
/// (no reparenting) and signal (a) does the work.
///
/// If the parent is still alive AND not a subreaper, or we can't determine the
/// parent (`None` → can't positively confirm orphanhood), or the process is too
/// young, we LEAVE IT ALONE. A live-real-parent MCP is an MCP with a live Claude
/// session attached — reaping it would silently break that session's channel
/// push, which is worse than the CPU burn we're backstopping. Conservative
/// defaults on a best-effort path: when we can't confirm the precondition, do
/// nothing.
///
/// `is_self` short-circuits to false so the launcher never reaps itself.
/// The MCP gate is [`is_mcp_server_argv`] over the process's argv.
pub fn steady_state_mcp_should_reap<S: AsRef<str>>(
    is_self: bool,
    argv: &[S],
    run_time_secs: u64,
    parent_pid: Option<u32>,
    parent_alive: Option<bool>,
    parent_is_subreaper: bool,
) -> bool {
    steady_state_orphan_signal(
        is_self,
        argv,
        run_time_secs,
        parent_pid,
        parent_alive,
        parent_is_subreaper,
    )
    .is_some()
}

/// WHICH orphan signal made the reaper's decision — so the log line states
/// the actual reason instead of always claiming a dead parent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrphanSignal {
    /// Signal (a): the known, non-subreaper parent is dead (the Windows case).
    ParentDead,
    /// Signal (b): reparented to a live subreaper (the POSIX case).
    ReparentedToSubreaper,
}

impl OrphanSignal {
    /// Human-readable reason for the reap log line.
    pub fn describe(self, parent_pid: Option<u32>) -> String {
        let ppid = parent_pid.map_or_else(|| "?".to_string(), |p| p.to_string());
        match self {
            OrphanSignal::ParentDead => format!("parent PID {ppid} is dead"),
            OrphanSignal::ReparentedToSubreaper => {
                format!("reparented to subreaper PID {ppid}")
            }
        }
    }
}

/// The orphan decision with its reason: `Some(signal)` when the process is
/// an orphaned MCP to reap, `None` to leave it alone. Same gates, in the
/// same order, as documented on [`steady_state_mcp_should_reap`].
pub fn steady_state_orphan_signal<S: AsRef<str>>(
    is_self: bool,
    argv: &[S],
    run_time_secs: u64,
    parent_pid: Option<u32>,
    parent_alive: Option<bool>,
    parent_is_subreaper: bool,
) -> Option<OrphanSignal> {
    if is_self {
        return None;
    }
    if !is_mcp_server_argv(argv) {
        return None;
    }
    if run_time_secs < STEADY_STATE_MCP_MIN_AGE_SECS {
        return None;
    }
    // No known parent → can't positively confirm orphanhood → leave alone.
    parent_pid?;
    // Orphan signal (b): reparented to a subreaper (the POSIX orphan case —
    // a healthy session-attached MCP is never parented to a subreaper).
    if parent_is_subreaper {
        return Some(OrphanSignal::ReparentedToSubreaper);
    }
    // Orphan signal (a): we definitively know the (non-subreaper) parent is dead
    // (the Windows case — Windows does not reparent orphans).
    matches!(parent_alive, Some(false)).then_some(OrphanSignal::ParentDead)
}

/// Is `parent_pid` a subreaper to which the OS reparents orphans? Cross-OS:
/// - POSIX: PID 1 (init) is the universal floor; `systemd --user` and other
///   `PR_SET_CHILD_SUBREAPER` reapers also qualify, identified by the parent's
///   executable/comm basename. We treat PID 1 always, plus a small basename
///   allowlist for the common user-session subreapers.
/// - Windows: no orphan reparenting exists → always false (signal (a) handles it).
///
/// `parent_comm` is the parent process's executable basename / comm (lowercased
/// by the caller is fine; we lowercase here defensively). Pass `""` when unknown
/// — then only the PID-1 check applies (still correct, just narrower).
pub fn parent_pid_is_subreaper(parent_pid: u32, parent_comm: &str) -> bool {
    #[cfg(windows)]
    {
        let _ = (parent_pid, parent_comm);
        false
    }
    #[cfg(not(windows))]
    {
        if parent_pid == 1 {
            return true;
        }
        // Common POSIX user-session subreapers an orphan can land on.
        let comm = parent_comm.to_ascii_lowercase();
        // Match a bare/whole comm to avoid catching e.g. "systemd-resolved".
        matches!(
            comm.as_str(),
            "systemd" | "init" | "launchd" | "systemd --user" | "(sd-pam)"
        )
    }
}

/// Everything [`steady_state_mcp_should_reap`] needs about one process, read
/// from a [`refresh_with_cmdlines`] snapshot. Split from the reaper loop so a
/// real-process test can assert what the reaper actually sees.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReapInputs {
    pub pid: u32,
    pub is_self: bool,
    /// The argv the MCP gate reads.
    pub argv: Vec<String>,
    /// `argv` joined, for the log line.
    pub cmdline: String,
    pub run_time_secs: u64,
    pub parent_pid: Option<u32>,
    pub parent_alive: Option<bool>,
    pub parent_is_subreaper: bool,
}

impl ReapInputs {
    /// Apply the orphan predicate to these inputs.
    pub(crate) fn should_reap(&self) -> bool {
        self.orphan_signal().is_some()
    }

    /// The orphan decision with its reason.
    pub(crate) fn orphan_signal(&self) -> Option<OrphanSignal> {
        steady_state_orphan_signal(
            self.is_self,
            &self.argv,
            self.run_time_secs,
            self.parent_pid,
            self.parent_alive,
            self.parent_is_subreaper,
        )
    }
}

/// Read the reaper's inputs for `proc` out of the snapshot `sys`.
pub(crate) fn reap_inputs(
    sys: &sysinfo::System,
    pid: sysinfo::Pid,
    proc: &sysinfo::Process,
    my_pid: u32,
) -> ReapInputs {
    use vct_launcher_core::process::pid_is_alive;

    let parent_pid = proc.parent().map(|p| p.as_u32());
    // Only probe parent liveness when there IS a parent — and never treat
    // the launcher's own pid as a "dead parent" (an MCP whose parent is the
    // launcher is NOT orphaned; we just don't spawn MCPs, but be defensive).
    let parent_alive = parent_pid.map(|ppid| {
        if ppid == my_pid {
            true
        } else {
            pid_is_alive(ppid)
        }
    });
    // Orphan signal (b) — POSIX reparenting. Resolve whether the parent is a
    // subreaper (PID 1 / systemd / launchd). On POSIX an orphan is reparented
    // to a live subreaper the instant its real parent exits, so signal (a)
    // (dead parent) is unreachable there; (b) catches it. A live session-
    // attached MCP's parent is the Claude process, never a subreaper, so this
    // never reaps a healthy MCP. Look up the parent process's exe basename
    // from the same snapshot; "" when the parent row isn't in the table
    // (then only the PID-1 check inside parent_pid_is_subreaper applies).
    let parent_is_subreaper = parent_pid
        .map(|ppid| {
            let comm = sys
                .process(sysinfo::Pid::from_u32(ppid))
                .map(|pp| {
                    pp.exe()
                        .and_then(|p| p.file_name())
                        .map(|n| n.to_string_lossy().into_owned())
                        .unwrap_or_else(|| pp.name().to_string_lossy().into_owned())
                })
                .unwrap_or_default();
            // The launcher itself is never a subreaper for our MCPs.
            ppid != my_pid && parent_pid_is_subreaper(ppid, &comm)
        })
        .unwrap_or(false);

    ReapInputs {
        pid: pid.as_u32(),
        is_self: pid.as_u32() == my_pid,
        argv: argv_of(proc),
        cmdline: joined_cmdline(proc),
        run_time_secs: proc.run_time(),
        parent_pid,
        parent_alive,
        parent_is_subreaper,
    }
}

/// The orphan reaper over an existing snapshot, terminate step injected (see
/// [`run_mcp_kill_sweep`]). Returns the number of orphans handed to
/// `terminate`.
fn run_orphaned_mcp_reap(
    sys: &sysinfo::System,
    my_pid: u32,
    terminate: &mut dyn FnMut(&sysinfo::Process),
) -> usize {
    let mut matched = 0usize;
    let mut orphans: Vec<(&sysinfo::Process, ReapInputs, OrphanSignal)> = Vec::new();
    for (pid, proc) in sys.processes() {
        let inputs = reap_inputs(sys, *pid, proc, my_pid);
        if is_mcp_server_argv(&inputs.argv) {
            matched += 1;
        }
        if let Some(signal) = inputs.orphan_signal() {
            orphans.push((proc, inputs, signal));
        }
    }
    // Counters first, so they are on record even if a terminate misbehaves.
    log_enumeration("steady-state reap", enumeration_stats(sys), matched);

    for (proc, inputs, signal) in &orphans {
        tracing::warn!(
            "[update_gate] steady-state reap: terminating ORPHANED MCP PID {} \
             ({}, age {}s, cmd: {})",
            inputs.pid,
            signal.describe(inputs.parent_pid),
            inputs.run_time_secs,
            inputs.cmdline.chars().take(120).collect::<String>()
        );
        terminate(proc);
    }
    orphans.len()
}

/// v0.2.71 (P6): best-effort steady-state reaper for ORPHANED coordination /
/// MCP processes whose controlling Claude session is gone.
///
/// WHY this exists: a coordination MCP that ignores stdin-EOF (the
/// `vct-coordination` server.py pre-v0.2.71 zombie-poller bug) survives session
/// close and accumulates one orphan per closed Claude surface, each burning CPU
/// polling Supabase forever. The server-side fix (cancel the poll loop on EOF)
/// makes such processes self-exit, so on a fixed install this reaper finds
/// NOTHING. It is defense-in-depth: it backstops any MCP — present or future —
/// that regresses to ignoring EOF, by reaping only processes we can POSITIVELY
/// confirm are orphaned (dead parent + past the spawn-race grace window).
///
/// Unlike `pre_update_mcp_kill_sweep` (which runs inside the update flow and
/// reaps ALL matching MCPs because the whole MCP fleet is about to respawn
/// against a fresh binary), this runs in STEADY STATE (e.g. launcher startup)
/// and must NOT touch MCPs with a live parent session. The orphan predicate
/// ([`steady_state_mcp_should_reap`]) enforces that. Its argv match reads
/// the [`refresh_with_cmdlines`] snapshot (argv exists only there).
///
/// Posture: [`terminate_mcp_server`] (SIGTERM on POSIX, never SIGKILL; a
/// `taskkill /T /F` tree kill on Windows) + soft-fail, never blocks boot.
/// Returns the number of orphans signalled (informational). Refuses under a
/// cargo test binary — a real orphaned MCP on the developer's machine is not
/// the test's to reap; tests drive [`run_orphaned_mcp_reap`] with an injected
/// terminate step.
pub fn steady_state_orphaned_mcp_reap() -> usize {
    if crate::hub_launcher::running_under_test_harness() {
        tracing::warn!(
            "[update_gate] steady-state reap: refusing to terminate MCP \
             processes — this process is a cargo test binary"
        );
        return 0;
    }
    let sys = process_table_with_cmdlines();
    run_orphaned_mcp_reap(&sys, std::process::id(), &mut terminate_mcp_server)
}

/// v0.2.59: return true if a process's executable BASENAME identifies it
/// as a `vct-hub` instance we should reap during an update.
///
/// Matched on the executable file name (`vct-hub` / `vct-hub.exe`), NOT
/// a cmdline substring — a substring like `"vct-hub"` would also hit
/// `vct-hub-notes.txt` in someone's args, or a `vct-hub --stop` helper
/// invocation. The exe basename is the precise signal: it IS a hub
/// binary. (Windows comparison is case-insensitive; POSIX is exact.)
///
/// Pure helper, isolated for unit testing — process enumeration is hard
/// to mock but the basename logic is deterministic.
pub fn hub_exe_basename_match(exe_basename: &str) -> bool {
    let b = exe_basename;
    #[cfg(target_os = "windows")]
    {
        let lower = b.to_ascii_lowercase();
        lower == "vct-hub.exe" || lower == "vct-hub"
    }
    #[cfg(not(target_os = "windows"))]
    {
        b == "vct-hub"
    }
}

/// One process the hub backstop sweep would terminate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HubSweepCandidate {
    pub pid: u32,
    pub exe_basename: String,
    /// For the log line only — the match is on `exe_basename`.
    pub cmdline: String,
}

/// The candidate filter of the hub backstop sweep: every process in `sys`
/// whose executable basename is a hub binary, never `my_pid`.
pub(crate) fn hub_sweep_candidates(sys: &sysinfo::System, my_pid: u32) -> Vec<HubSweepCandidate> {
    sys.processes()
        .iter()
        .filter(|(pid, _)| pid.as_u32() != my_pid)
        .filter_map(|(pid, proc)| {
            // Identify by executable basename, not cmdline substring.
            let exe_basename = proc
                .exe()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            hub_exe_basename_match(&exe_basename).then(|| HubSweepCandidate {
                pid: pid.as_u32(),
                exe_basename,
                cmdline: joined_cmdline(proc),
            })
        })
        .collect()
}

/// v0.2.59: backstop sweep for `vct-hub` processes the single-`hub.pid`
/// stop path (`ensure_hub_stopped_for_update`) could not see.
///
/// WHY this exists: `ensure_hub_stopped_for_update` reads exactly ONE
/// lockfile — `<vct_root_dir()>/hub.pid` — and stops only the pid it
/// names. But a `vct-hub` started outside THAT lockfile's protocol is
/// invisible to it and to `vct-hub --stop`:
///   * a dev `cargo run`/`--foreground` hub from a different checkout,
///   * a hub from a SECOND install root (different `VCT_STATE_DIR` →
///     different `hub.pid`),
///   * a hub that survived a crash which cleared/overwrote the pid.
///
/// Any such hub keeps `~/.vct/launcher.db` open (blocking DB writes)
/// and, on Windows, keeps `vct-hub.exe` locked (blocking the binary
/// swap). This sweep is the process-identity backstop: after the
/// lockfile-driven stop, terminate any REMAINING hub binary.
///
/// Posture (per the 2026-06-15 decision): [`terminate_process`] — SIGTERM
/// (graceful) on POSIX, a single-process forced kill on Windows, never the
/// tree (unlike the MCP sweep) — + soft-fail. We never SIGKILL here and we
/// never block the update if one survives; the pre-pull binary rename +
/// the update-gate lockfile already backstop the Windows path, and
/// SQLite is crash-safe so a surviving reader is at worst a transient
/// lock the next write retries past.
///
/// SAFETY: strictly filtered to processes whose EXE BASENAME is
/// `vct-hub`/`vct-hub.exe` (never a cmdline substring), and never our
/// own pid (the launcher). Returns the number of hubs signalled
/// (informational).
pub fn pre_update_hub_kill_sweep() -> usize {
    // v0.2.92 — REAPING HALF of the test-contamination defect.
    //
    // This sweep identifies its targets by PROCESS IDENTITY (exe basename),
    // so `VCT_STATE_DIR` isolation cannot contain it: a test with a perfectly
    // redirected state dir still SIGTERMs the developer's real running hub.
    // Six tests in `installer.rs::hub_stop_tests` call
    // `ensure_hub_stopped_for_update`, which calls this unconditionally —
    // which is why `cargo test -p vct-launcher-temp --lib` was observed
    // stopping the live hub and removing `~/.vct/hub.pid` (the pid file is
    // deleted by the dying hub itself, during its own graceful shutdown).
    //
    // Measured: with a decoy process named `vct-hub` running inside the same
    // PID namespace as the suite, `cargo test --workspace` reaped it. With
    // this guard, it survives.
    //
    // Returning 0 is honest: under a test harness we sweep nothing, and no
    // caller reads the count for anything but a log line.
    if crate::hub_launcher::running_under_test_harness() {
        tracing::warn!(
            "[update_gate] hub-sweep: refusing to enumerate/terminate \
             vct-hub processes — this process is a cargo test binary, and \
             the sweep matches on exe basename, so it would reap the \
             developer's REAL hub regardless of VCT_STATE_DIR isolation"
        );
        return 0;
    }
    let sys = process_table_with_cmdlines();
    let candidates = hub_sweep_candidates(&sys, std::process::id());
    let mut count = 0usize;
    for cand in &candidates {
        let Some(proc) = sys.process(sysinfo::Pid::from_u32(cand.pid)) else {
            continue;
        };
        tracing::info!(
            "[update_gate] hub-sweep: terminating stray vct-hub PID {} (exe: {}, cmd: {})",
            cand.pid,
            cand.exe_basename,
            cand.cmdline.chars().take(120).collect::<String>()
        );
        terminate_process(proc);
        count += 1;
    }
    count
}

/// v0.2.63: resolve a running process's executable PATH by pid, via the same
/// snapshot (`refresh_with_cmdlines`) the sweeps above use. Returns `None` if
/// the pid is gone or the platform won't expose the exe path (permissions,
/// etc.).
///
/// Used by the launcher's boot-time hub-identity check
/// ([`crate::hub_launcher::ensure_hub_running`]) to decide whether a running
/// hub is the launcher's install-folder copy or a foreign/stale binary (a dev
/// `cargo run`, a different checkout, an old install) that must be swapped.
/// Callers MUST treat `None` as "can't tell — leave it alone" (no false kills).
pub fn process_exe_by_pid(pid: u32) -> Option<std::path::PathBuf> {
    // Whole-table refresh through the one helper rather than the single-pid
    // refresh API, so this lookup sees exactly what the sweeps see.
    let sys = process_table_with_cmdlines();
    sys.process(sysinfo::Pid::from_u32(pid))
        .and_then(|proc| proc.exe().map(|e| e.to_path_buf()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir() -> tempfile::TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    // ── v0.2.63: process_exe_by_pid (boot-time hub-identity check) ───────

    #[test]
    fn process_exe_by_pid_resolves_own_exe() {
        // Our own process's exe path is always resolvable on every supported
        // OS (Linux /proc/self/exe, macOS libproc, Windows OpenProcess) — this
        // proves the sysinfo backend works in the test environment.
        let got = process_exe_by_pid(std::process::id());
        assert!(got.is_some(), "own pid must resolve to an exe path");
    }

    #[test]
    fn process_exe_by_pid_none_for_dead_pid() {
        // u32::MAX is never a live pid → no process → None.
        assert_eq!(process_exe_by_pid(u32::MAX), None);
    }

    // ── Lockfile round-trip ─────────────────────────────────────────────

    #[test]
    fn write_then_read_returns_payload() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        write_lockfile_at(&p, Phase::GitPull, 15).unwrap();
        let data = read_lockfile_at(&p).expect("payload");
        assert_eq!(data.phase, Phase::GitPull);
        assert_eq!(data.started_by_pid, std::process::id());
        assert!(!data.started_at.is_empty());
        assert!(!data.expected_completion_by.is_empty());
    }

    #[test]
    fn write_creates_parent_dir() {
        let d = tmpdir();
        let p = d.path().join("nested").join("dir").join("lock.json");
        assert!(!p.parent().unwrap().exists());
        write_lockfile_at(&p, Phase::GitPull, 15).unwrap();
        assert!(p.exists());
    }

    #[test]
    fn read_missing_returns_none() {
        let d = tmpdir();
        let p = d.path().join("nope.json");
        assert!(read_lockfile_at(&p).is_none());
    }

    #[test]
    fn read_corrupt_returns_none() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        fs::write(&p, b"{ not valid json").unwrap();
        assert!(read_lockfile_at(&p).is_none());
    }

    #[test]
    fn delete_missing_returns_true() {
        let d = tmpdir();
        let p = d.path().join("absent.json");
        assert!(delete_lockfile_at(&p));
    }

    // ── is_update_in_progress decision matrix ───────────────────────────

    #[test]
    fn no_lockfile_means_no_update() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        assert!(!is_update_in_progress_at(&p));
    }

    #[test]
    fn fresh_lockfile_means_update_in_progress() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        write_lockfile_at(&p, Phase::GitPull, 10).unwrap();
        assert!(is_update_in_progress_at(&p));
    }

    // ── v0.2.60 poller stand-down gate ──────────────────────────────────

    #[test]
    fn skip_gate_true_when_update_in_progress() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        write_lockfile_at(&p, Phase::InstallPy, 10).unwrap();
        assert!(
            skip_if_update_in_progress_at("test-poller", &p),
            "a fresh in-progress lockfile must make pollers stand down"
        );
    }

    #[test]
    fn skip_gate_false_when_no_update() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        assert!(
            !skip_if_update_in_progress_at("test-poller", &p),
            "no lockfile → pollers run normally"
        );
    }

    #[test]
    fn skip_gate_false_when_lockfile_stale() {
        // A stale (deadline-passed) lockfile must NOT wedge pollers off
        // forever — it's treated as no-update, same as is_update_in_progress.
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        let past = (Utc::now() - ChronoDuration::minutes(10))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let payload = LockfilePayload {
            started_at: past.clone(),
            started_by_pid: 1,
            phase: Phase::InstallPy,
            expected_completion_by: past,
        };
        fs::write(&p, serde_json::to_string(&payload).unwrap()).unwrap();
        assert!(!skip_if_update_in_progress_at("test-poller", &p));
    }

    #[test]
    fn stale_lockfile_means_no_update() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        let past = (Utc::now() - ChronoDuration::minutes(10))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let payload = LockfilePayload {
            started_at: past.clone(),
            started_by_pid: 1,
            phase: Phase::GitPull,
            expected_completion_by: past,
        };
        fs::write(&p, serde_json::to_string(&payload).unwrap()).unwrap();
        assert!(!is_update_in_progress_at(&p));
    }

    // ── cleanup_if_stale ────────────────────────────────────────────────

    #[test]
    fn cleanup_removes_stale_lockfile() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        let past = (Utc::now() - ChronoDuration::minutes(30))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let payload = LockfilePayload {
            started_at: past.clone(),
            started_by_pid: 1,
            phase: Phase::BinaryRefresh,
            expected_completion_by: past,
        };
        fs::write(&p, serde_json::to_string(&payload).unwrap()).unwrap();
        assert!(cleanup_if_stale_at(&p));
        assert!(!p.exists());
    }

    #[test]
    fn cleanup_leaves_fresh_lockfile() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        write_lockfile_at(&p, Phase::GitPull, 15).unwrap();
        assert!(!cleanup_if_stale_at(&p));
        assert!(p.exists());
    }

    #[test]
    fn cleanup_when_absent_is_noop() {
        let d = tmpdir();
        let p = d.path().join("absent.json");
        assert!(!cleanup_if_stale_at(&p));
    }

    // ── Guard RAII ──────────────────────────────────────────────────────

    #[test]
    fn guard_writes_on_new_and_deletes_on_drop() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        {
            let (_guard, res) = UpdateInProgressGuard::new_at(p.clone(), 15);
            res.expect("initial write");
            assert!(p.exists(), "guard should write lockfile on new");
        }
        // After drop, lockfile is gone.
        assert!(!p.exists(), "guard should delete lockfile on drop");
    }

    #[test]
    fn guard_advance_phase_updates_disk() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        let (mut guard, res) = UpdateInProgressGuard::new_at(p.clone(), 15);
        res.unwrap();
        guard.advance_phase(Phase::InstallPy);
        let data = read_lockfile_at(&p).unwrap();
        assert_eq!(data.phase, Phase::InstallPy);
        drop(guard);
        assert!(!p.exists());
    }

    #[test]
    fn guard_disarm_and_cleanup_idempotent() {
        let d = tmpdir();
        let p = d.path().join(LOCKFILE_BASENAME);
        let (mut guard, res) = UpdateInProgressGuard::new_at(p.clone(), 15);
        res.unwrap();
        guard.disarm_and_cleanup();
        assert!(!p.exists());
        // Drop after disarm — must not error.
        drop(guard);
    }

    // ── is_mcp_server_argv: EXECUTING an MCP vs merely citing one ────────

    fn words(cmd: &str) -> Vec<&str> {
        cmd.split_whitespace().collect()
    }

    /// Real MCP server launches — every one must match. Includes the shapes
    /// running on a dev machine today and what VCO registers.
    const MCP_SERVER_SHAPES: &[&[&str]] = &[
        &["/home/x/.venv/bin/python", "/home/x/claude_mcp_servers/weaviate_mcp/server.py"],
        &["/home/x/.venv/bin/python", "/home/x/claude_mcp_servers/vct_coordination_mcp/server.py"],
        &["/home/x/.venv/bin/python", "-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
        &["/home/x/.venv/bin/python", "-m", "claude_mcp_servers.wrappers.excalidraw_proxy"],
        &["python3", "-u", "-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
        &["python3.12", "-X", "utf8", "/x/claude_mcp_servers/weaviate_mcp/server.py"],
        &["python", "-um", "claude_mcp_servers.wrappers.mermaid_proxy"],
        &[r"C:\vco\.venv\Scripts\python.exe", r"C:\vco\claude_mcp_servers\weaviate_mcp\server.py"],
        &["npx", "-y", "@playwright/mcp@latest"],
        &["npm", "exec", "@playwright/mcp@latest"],
        &["npx", "--package=@playwright/mcp", "playwright-mcp"],
        &["npm", "exec", "-p", "@upstash/context7-mcp", "--", "context7-mcp"],
        &["node", "/home/x/.npm/_npx/9833c18b2d85bc59/node_modules/.bin/playwright-mcp"],
        &["npx", "-y", "@upstash/context7-mcp@latest"],
        &["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        &["node", "/usr/lib/node_modules/@modelcontextprotocol/server-everything/dist/index.js"],
        &[
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files\nodejs\node_modules\npm\bin\npx-cli.js",
            "-y",
            "@playwright/mcp@latest",
        ],
        &["/bin/bash", "/home/x/claude_mcp_servers/search_mcp/wrapper.sh"],
        &["uv", "run", "--directory", "/x", "/x/claude_mcp_servers/weaviate_mcp/server.py"],
        &["uv", "run", "python", "-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
        &["pwsh", "-NoProfile", "-File", r"C:\vco\claude_mcp_servers\x\wrapper.ps1"],
    ];

    /// Processes that only CITE an MCP path or package, or are unrelated —
    /// none may match.
    const LEAVE_ALONE_SHAPES: &[&[&str]] = &[
        &["vim", "/home/x/claude_mcp_servers/weaviate_mcp/server.py"],
        &["tail", "-f", "/home/x/claude_mcp_servers/logs/mcp.log"],
        &["less", "/home/x/claude_mcp_servers/README.md"],
        &["grep", "-r", "claude_mcp_servers", "/home/x"],
        &["code", "/home/x/claude_mcp_servers"],
        &["bash", "-c", "cd /home/x/claude_mcp_servers && ls"],
        &["/bin/sh", "-lc", "python /home/x/claude_mcp_servers/weaviate_mcp/server.py"],
        &["python3", "-c", "import claude_mcp_servers.wrappers.mermaid_proxy"],
        &["node", "-e", "require('@modelcontextprotocol/sdk')"],
        &["npm", "install", "@modelcontextprotocol/server-filesystem"],
        &["npm", "list", "file:vco_lib/excalidraw_mcp_fork"],
        &["sh", "-c", "playwright-mcp"],
        &["npx", "-c", "playwright-mcp"],
        &["npx", "--package=@playwright/mcp", "eslint", "."],
        &["npm", "exec", "-p", "@upstash/context7-mcp", "--", "prettier", "x.js"],
        &["npx", "-p", "@modelcontextprotocol/inspector"],
        &["/usr/bin/python3", "/home/user/myproject/main.py", "--port", "8000"],
        &["/usr/bin/python3", "-m", "http.server"],
        &["python3", "/home/x/notes/claude_mcp_servers.py"],
        &["node", "/home/user/another-app/server.js"],
        &["/home/x/.vct/dist/vct-launcher"],
        &["/home/x/.vct/dist/vct-hub", "--start-if-not-running"],
        &["pwsh", "-Command", r"Get-Content C:\vco\claude_mcp_servers\x.log"],
        &["/home/x/.venv/bin/python", "/x/claude_mcp_servers"],
        &[],
    ];

    #[test]
    fn mcp_server_argv_matches_every_real_mcp_launch_shape() {
        for argv in MCP_SERVER_SHAPES {
            assert!(is_mcp_server_argv(argv), "should match (executes an MCP): {argv:?}");
        }
    }

    #[test]
    fn mcp_server_argv_leaves_citing_and_unrelated_processes_alone() {
        for argv in LEAVE_ALONE_SHAPES {
            assert!(!is_mcp_server_argv(argv), "must NOT match (only cites / unrelated): {argv:?}");
        }
    }

    #[test]
    fn mcp_server_argv_excalidraw_node_server_stays_out_of_scope() {
        // Behaviour kept from before v0.2.102: the excalidraw node server was
        // never a sweep target (no pattern in its argv); its python proxy is.
        assert!(!is_mcp_server_argv(&words(
            "/home/x/.local/bin/node /home/x/VCO/vco_lib/excalidraw_mcp_fork/dist/mcp/index.bundled.js"
        )));
    }

    #[test]
    fn npm_package_name_strips_the_version() {
        assert_eq!(npm_package_name("@playwright/mcp@latest"), "@playwright/mcp");
        assert_eq!(npm_package_name("@playwright/mcp"), "@playwright/mcp");
        assert_eq!(npm_package_name("left-pad@1.0.0"), "left-pad");
        assert!(!is_mcp_npm_package("@playwright/mcp-extra@1"));
    }

    // ── hub_exe_basename_match (v0.2.59 backstop sweep) ─────────────────

    #[test]
    fn hub_match_accepts_bare_basename() {
        // The exe basename sysinfo reports for an installed hub.
        assert!(hub_exe_basename_match("vct-hub"));
    }

    #[test]
    fn hub_match_rejects_launcher_and_unrelated() {
        // The launcher itself must never be swept (we exclude our own
        // pid too, but the basename filter is the first line of defense).
        assert!(!hub_exe_basename_match("vct-launcher"));
        assert!(!hub_exe_basename_match("vct-updater"));
        assert!(!hub_exe_basename_match("python3"));
        assert!(!hub_exe_basename_match(""));
        // NOT a substring matcher: a hub-ish-looking but distinct binary
        // name must not match.
        assert!(!hub_exe_basename_match("vct-hub-notes"));
        assert!(!hub_exe_basename_match("my-vct-hub"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn hub_match_windows_is_case_insensitive_and_accepts_exe() {
        assert!(hub_exe_basename_match("vct-hub.exe"));
        assert!(hub_exe_basename_match("VCT-HUB.EXE"));
        assert!(hub_exe_basename_match("Vct-Hub"));
        assert!(!hub_exe_basename_match("vct-hub-notes.exe"));
    }

    #[cfg(not(target_os = "windows"))]
    #[test]
    fn hub_match_posix_is_exact() {
        // POSIX exe basenames are case-sensitive and carry no `.exe`.
        assert!(!hub_exe_basename_match("vct-hub.exe"));
        assert!(!hub_exe_basename_match("VCT-HUB"));
    }

    // ── steady_state_mcp_should_reap (v0.2.71 P6 orphan predicate) ───────
    //
    // We test the DECISION (per the "test the act AND the leave-alone case"
    // rule): an orphaned MCP IS reaped; every non-orphan / can't-confirm case
    // is LEFT ALONE.

    const COORD_CMD: &[&str] =
        &["/x/.venv/bin/python", "/x/claude_mcp_servers/vct_coordination_mcp/server.py"];
    const USER_PY: &[&str] = &["/usr/bin/python3", "/home/user/myproject/main.py"];

    #[test]
    fn reap_when_parent_dead_and_old_enough() {
        // Signal (a) — dead parent (the Windows case; POSIX reparents so this is
        // rare there). An MCP server argv, past the grace window, parent dead → REAP.
        assert!(steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            STEADY_STATE_MCP_MIN_AGE_SECS, // exactly at the floor counts
            Some(424242),
            Some(false),
            false, // not a subreaper — signal (a) does the work
        ));
        assert!(steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            6 * 3600, // 6h old (field signature)
            Some(424242),
            Some(false),
            false,
        ));
    }

    #[test]
    fn reap_when_reparented_to_subreaper_posix() {
        // Signal (b) — the POSIX orphan case the v0.2.71 review surfaced: the
        // kernel reparents the orphan to a LIVE subreaper, so parent_alive is
        // Some(true) yet it IS orphaned. parent_is_subreaper=true → REAP.
        assert!(steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            6 * 3600,
            Some(1),          // reparented to init/PID-1
            Some(true),       // the subreaper is alive — signal (a) would MISS this
            true,             // ...but signal (b) catches it
        ));
        // Also at the age floor.
        assert!(steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            STEADY_STATE_MCP_MIN_AGE_SECS,
            Some(1),
            Some(true),
            true,
        ));
    }

    #[test]
    fn leave_alone_when_parent_alive_and_not_subreaper() {
        // A live REAL parent == a live Claude session attached. NEVER reap — and
        // crucially, parent_is_subreaper=false means this is NOT a reparented
        // orphan, so signal (b) must not fire either.
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            6 * 3600,
            Some(12345),
            Some(true),
            false, // real session parent, not a subreaper → leave alone
        ));
    }

    #[test]
    fn subreaper_signal_still_respects_mcp_pattern_age_and_self() {
        // Signal (b) does NOT bypass the other guards — a non-MCP / too-young /
        // self process parented to a subreaper is STILL left alone.
        assert!(!steady_state_mcp_should_reap(
            false,
            USER_PY, // not an MCP
            6 * 3600,
            Some(1),
            Some(true),
            true,
        ));
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            STEADY_STATE_MCP_MIN_AGE_SECS - 1, // too young (fork-race window)
            Some(1),
            Some(true),
            true,
        ));
        assert!(!steady_state_mcp_should_reap(
            true, // is_self short-circuits even under the subreaper signal
            COORD_CMD,
            6 * 3600,
            Some(1),
            Some(true),
            true,
        ));
    }

    #[test]
    fn leave_alone_when_parent_unknown() {
        // Can't determine the parent → can't positively confirm orphanhood →
        // do nothing (conservative default on a best-effort path). Neither signal.
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            6 * 3600,
            None,
            None,
            false,
        ));
        // parent pid present but liveness undeterminable + not a subreaper also
        // leaves it alone.
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            6 * 3600,
            Some(999),
            None,
            false,
        ));
    }

    #[test]
    fn leave_alone_when_too_young() {
        // A just-spawned MCP can momentarily look parent-less while the harness
        // wires up the parent link. The grace window prevents a fork-race kill.
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            STEADY_STATE_MCP_MIN_AGE_SECS - 1,
            Some(424242),
            Some(false),
            false,
        ));
        assert!(!steady_state_mcp_should_reap(
            false,
            COORD_CMD,
            0,
            Some(424242),
            Some(false),
            false,
        ));
    }

    #[test]
    fn leave_alone_when_not_an_mcp() {
        // Unrelated user processes must survive even when orphaned + old.
        assert!(!steady_state_mcp_should_reap(
            false,
            USER_PY,
            6 * 3600,
            Some(424242),
            Some(false),
            false,
        ));
    }

    #[test]
    fn never_reap_self() {
        // is_self short-circuits regardless of every other signal.
        assert!(!steady_state_mcp_should_reap(
            true,
            COORD_CMD,
            6 * 3600,
            Some(424242),
            Some(false),
            false,
        ));
    }

    #[test]
    fn parent_pid_is_subreaper_matrix() {
        // PID 1 is always a subreaper on POSIX; on Windows nothing is.
        #[cfg(not(windows))]
        {
            assert!(parent_pid_is_subreaper(1, ""));
            assert!(parent_pid_is_subreaper(1, "anything"));
            assert!(parent_pid_is_subreaper(2589, "systemd"));
            assert!(parent_pid_is_subreaper(500, "launchd"));
            // A normal parent process is NOT a subreaper.
            assert!(!parent_pid_is_subreaper(12345, "node"));
            assert!(!parent_pid_is_subreaper(12345, "claude"));
            // Don't be fooled by a prefix match.
            assert!(!parent_pid_is_subreaper(12345, "systemd-resolved"));
        }
        #[cfg(windows)]
        {
            // Windows does not reparent orphans → never a subreaper.
            assert!(!parent_pid_is_subreaper(1, "anything"));
            assert!(!parent_pid_is_subreaper(4, "System"));
        }
    }

    // ── steady_state_orphaned_mcp_reap runner: soft-fail / no-panic ──────
    //
    // The public runner refuses under a cargo test binary (a real orphaned MCP
    // on the developer's machine is not the suite's to reap), so these drive
    // the same core it runs — `run_orphaned_mcp_reap` over a live
    // `refresh_with_cmdlines` snapshot — with a terminate step that RECORDS and
    // never signals. That keeps the contract that matters for boot safety (no
    // panic over the real process table, never targets itself) without letting
    // the suite kill anything.

    #[test]
    fn reaper_runner_is_no_panic_and_returns_count() {
        let sys = process_table_with_cmdlines();
        let mut targeted = Vec::new();
        let reaped = run_orphaned_mcp_reap(&sys, std::process::id(), &mut |p| {
            targeted.push(p.pid().as_u32())
        });
        assert_eq!(reaped, targeted.len());
        assert!(
            reaped < 100_000,
            "reaper returned an implausible count ({reaped}) — likely a runaway match"
        );
    }

    #[test]
    fn reaper_runner_never_reaps_own_process() {
        let me = std::process::id();
        let sys = process_table_with_cmdlines();
        let mut targeted = Vec::new();
        let _ = run_orphaned_mcp_reap(&sys, me, &mut |p| targeted.push(p.pid().as_u32()));
        assert!(!targeted.contains(&me), "the reaper targeted its own process");
        assert!(pid_alive_self_sanity());
    }

    #[test]
    fn public_sweeps_refuse_under_the_test_harness() {
        // Both entry points match every process on the machine; under a cargo
        // test binary they must sweep nothing (the developer's live MCPs).
        assert!(crate::hub_launcher::running_under_test_harness());
        assert_eq!(pre_update_mcp_kill_sweep(), 0);
        assert_eq!(steady_state_orphaned_mcp_reap(), 0);
    }

    /// Local helper: confirm our own pid still reports alive after the reaper
    /// ran (re-uses the same liveness probe the runner uses for parents).
    fn pid_alive_self_sanity() -> bool {
        vct_launcher_core::process::pid_is_alive(std::process::id())
    }

    // ── v0.2.102 (S16-1): the sweeps see REAL processes' cmdlines ────────
    //
    // The pure-helper tests above stayed green while every sweep was a silent
    // no-op (the snapshot carried no cmdlines). These spawn a real child —
    // this test binary itself, re-run as a long sleeper — and drive the
    // enumeration the sweeps use. An "MCP server" child is the test binary
    // linked under a runtime name (`python3` / `python.exe`) whose first
    // argument is a `claude_mcp_servers/…/server.py` path, so its argv has
    // exactly the shape `is_mcp_server_argv` accepts. Same code on every OS
    // (the Windows CI leg runs them unchanged). They only ever terminate the
    // processes they spawned.

    const PROBE_SLEEP_ENV: &str = "VCT_W1B_PROBE_SLEEP";
    const PROBE_GRANDCHILD_PIDFILE_ENV: &str = "VCT_W1B_PROBE_GRANDCHILD_PIDFILE";
    const PROBE_TEST_NAME: &str = "commands::update_gate::tests::w1b_probe_sleeper";
    const PROBE_HARNESS_ARGS: &[&str] =
        &["--ignored", "--exact", "--test-threads", "1", "-q", PROBE_TEST_NAME];

    /// Not a test of its own: the body of the probe child. A no-op unless the
    /// spawning test set the env var. With the pidfile env var it first spawns
    /// one grandchild sleeper and writes that pid to the file.
    #[test]
    #[ignore = "probe child body for the real-process sweep tests"]
    // The grandchild is deliberately never waited on: this process sleeps
    // until the test kills it, and the test reaps the grandchild by pid.
    #[allow(clippy::zombie_processes)]
    fn w1b_probe_sleeper() {
        if std::env::var_os(PROBE_SLEEP_ENV).is_none() {
            return;
        }
        if let Some(pidfile) = std::env::var_os(PROBE_GRANDCHILD_PIDFILE_ENV) {
            let grandchild = std::process::Command::new(std::env::current_exe().unwrap())
                .arg("w1b-grandchild")
                .args(PROBE_HARNESS_ARGS)
                .env(PROBE_SLEEP_ENV, "1")
                .env_remove(PROBE_GRANDCHILD_PIDFILE_ENV)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("spawn grandchild");
            std::fs::write(pidfile, grandchild.id().to_string()).expect("write pidfile");
        }
        std::thread::sleep(std::time::Duration::from_secs(300));
    }

    /// A spawned probe child, terminated and reaped on drop.
    struct ProbeChild(std::process::Child);

    impl ProbeChild {
        fn pid(&self) -> u32 {
            self.0.id()
        }

        /// Wait up to 10 s for the child to exit.
        fn wait_exit(&mut self) -> Option<std::process::ExitStatus> {
            for _ in 0..100 {
                if let Some(s) = self.0.try_wait().expect("try_wait") {
                    return Some(s);
                }
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            None
        }
    }

    impl Drop for ProbeChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    /// A process we did not spawn directly (a grandchild): killed on drop.
    struct PidGuard(u32);

    impl Drop for PidGuard {
        fn drop(&mut self) {
            let mut sys = sysinfo::System::new();
            let pid = sysinfo::Pid::from_u32(self.0);
            sys.refresh_processes(sysinfo::ProcessesToUpdate::Some(&[pid]), true);
            if let Some(p) = sys.process(pid) {
                p.kill();
            }
        }
    }

    /// This test binary hard-linked (copied as a fallback) under `name`, in a
    /// temp dir next to it so the link stays on one filesystem. Declare it
    /// BEFORE the child that runs it, so the child drops (dies) first.
    struct LinkedExe {
        path: std::path::PathBuf,
        _dir: tempfile::TempDir,
    }

    fn linked_test_exe(name: &str) -> LinkedExe {
        let exe = std::env::current_exe().expect("current_exe");
        let dir = tempfile::tempdir_in(exe.parent().expect("exe dir")).expect("tempdir");
        let path = dir.path().join(name);
        if std::fs::hard_link(&exe, &path).is_err() {
            std::fs::copy(&exe, &path).expect("copy linked exe");
        }
        LinkedExe { path, _dir: dir }
    }

    /// Spawn `exe` as the sleeper with `marker` as its FIRST argument (where a
    /// runtime's script path goes); libtest treats it as one more filter.
    fn spawn_probe_from(exe: &std::path::Path, marker: &str, env: &[(&str, &str)]) -> ProbeChild {
        let mut cmd = std::process::Command::new(exe);
        cmd.arg(marker)
            .args(PROBE_HARNESS_ARGS)
            .env(PROBE_SLEEP_ENV, "1")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        for (k, v) in env {
            cmd.env(k, v);
        }
        ProbeChild(cmd.spawn().expect("spawn probe child"))
    }

    /// The test binary under its own (non-runtime) name.
    fn spawn_probe(marker: &str) -> ProbeChild {
        spawn_probe_from(&std::env::current_exe().expect("current_exe"), marker, &[])
    }

    /// A `claude_mcp_servers/<probe>/server.py` path, unique to this test.
    fn mcp_script_marker(tag: &str) -> String {
        let m = format!("/w1b/claude_mcp_servers/w1b_probe_{}_{}/server.py", tag, std::process::id());
        assert!(is_claude_mcp_script_path(&m));
        m
    }

    fn non_mcp_marker(tag: &str) -> String {
        format!("w1b-leave-alone-probe-{}-{}", tag, std::process::id())
    }

    /// The test binary linked as a Python runtime.
    fn python_runtime_exe() -> LinkedExe {
        linked_test_exe(if cfg!(windows) { "python.exe" } else { "python3" })
    }

    /// Spawn an "MCP server": the Python-named link running an MCP script path.
    fn spawn_mcp_probe(py: &LinkedExe, marker: &str, env: &[(&str, &str)]) -> ProbeChild {
        let argv0 = py.path.to_string_lossy().into_owned();
        assert!(is_mcp_server_argv(&[argv0.as_str(), marker]));
        spawn_probe_from(&py.path, marker, env)
    }

    /// Take snapshots (through the sweeps' own helper) until `pid` shows the
    /// expected cmdline marker, or give up after ~5 s and return the last one.
    fn snapshot_seeing(pid: u32, marker: &str) -> sysinfo::System {
        let mut sys = process_table_with_cmdlines();
        for _ in 0..50 {
            let seen = sys
                .process(sysinfo::Pid::from_u32(pid))
                .map(|p| joined_cmdline(p).contains(marker))
                .unwrap_or(false);
            if seen {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
            sys = process_table_with_cmdlines();
        }
        sys
    }

    #[test]
    fn mcp_kill_sweep_enumeration_sees_matching_child_cmdline() {
        let py = python_runtime_exe();
        let marker = mcp_script_marker("act");
        let child = spawn_mcp_probe(&py, &marker, &[]);
        let sys = snapshot_seeing(child.pid(), &marker);

        let stats = enumeration_stats(&sys);
        assert!(
            stats.with_cmdline > 0,
            "snapshot carries no cmdlines at all ({stats:?}) — refresh_with_cmdlines \
             does not request them"
        );
        let candidates = mcp_kill_sweep_candidates(&sys, std::process::id());
        let hit = candidates.iter().find(|c| c.pid == child.pid());
        assert!(
            hit.is_some(),
            "the kill sweep's candidate set misses a live child executing an MCP \
             server script ({} candidates, {stats:?})",
            candidates.len()
        );
        assert!(hit.unwrap().cmdline.contains(&marker));
        assert!(
            !candidates.iter().any(|c| c.pid == std::process::id()),
            "the sweep must never list its own process"
        );
    }

    #[test]
    fn mcp_kill_sweep_leaves_a_non_interpreter_citing_an_mcp_path_alone() {
        // The editor/`tail` case, as a real process: argv[1] IS a
        // claude_mcp_servers script path (the old substring filter would have
        // killed it), but argv[0] is not a runtime — nothing executes it.
        let marker = mcp_script_marker("cite");
        let child = spawn_probe(&marker);
        let sys = snapshot_seeing(child.pid(), &marker);

        // Not vacuous: the sweep DID read this child's argv, and rejected it.
        let proc = sys
            .process(sysinfo::Pid::from_u32(child.pid()))
            .expect("child present in the snapshot");
        assert!(
            joined_cmdline(proc).contains(&marker),
            "the snapshot did not read the child's cmdline, so the leave-alone \
             decision was never actually made"
        );
        let candidates = mcp_kill_sweep_candidates(&sys, std::process::id());
        assert!(
            !candidates.iter().any(|c| c.pid == child.pid()),
            "a process that only cites an MCP path must not be a candidate"
        );
    }

    #[test]
    fn mcp_kill_sweep_terminate_step_reaches_matching_child() {
        let py = python_runtime_exe();
        let marker = mcp_script_marker("term");
        let mut child = spawn_mcp_probe(&py, &marker, &[]);
        let child_pid = child.pid();
        let sys = snapshot_seeing(child_pid, &marker);

        // Terminate ONLY our own child through the production terminate step;
        // every other candidate (a real MCP on this machine) is just recorded.
        let mut terminated = Vec::new();
        let count = run_mcp_kill_sweep(&sys, std::process::id(), &mut |p| {
            if p.pid().as_u32() == child_pid {
                terminate_mcp_server(p);
                terminated.push(child_pid);
            }
        });
        assert_eq!(terminated, vec![child_pid], "terminate step never reached the child");
        assert!(count >= 1);

        let status = child
            .wait_exit()
            .expect("child still running 10 s after the sweep terminated it");
        assert!(!status.success());
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            assert_eq!(status.signal(), Some(15), "expected SIGTERM, got {status:?}");
        }
    }

    /// Spawn an MCP probe that has one grandchild; return both.
    fn spawn_mcp_probe_with_grandchild(py: &LinkedExe, tag: &str) -> (ProbeChild, PidGuard, String) {
        let marker = mcp_script_marker(tag);
        let pidfile = py.path.with_file_name(format!("grandchild-{tag}.pid"));
        let pidfile_s = pidfile.to_string_lossy().into_owned();
        let child = spawn_mcp_probe(py, &marker, &[(PROBE_GRANDCHILD_PIDFILE_ENV, &pidfile_s)]);
        let mut grandchild = None;
        for _ in 0..100 {
            if let Some(pid) = std::fs::read_to_string(&pidfile)
                .ok()
                .and_then(|t| t.trim().parse::<u32>().ok())
            {
                grandchild = Some(pid);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let grandchild = PidGuard(grandchild.expect("probe never reported its grandchild"));
        (child, grandchild, marker)
    }

    #[cfg(unix)]
    #[test]
    fn terminate_mcp_server_posix_signals_only_the_matched_process() {
        // The documented POSIX contract: SIGTERM to the matched process, no
        // tree walk (npm exec forwards SIGTERM; its node child is matched on
        // its own). The grandchild must survive this call.
        use vct_launcher_core::process::pid_is_alive;
        let py = python_runtime_exe();
        let (mut child, grandchild, marker) = spawn_mcp_probe_with_grandchild(&py, "posix");
        let sys = snapshot_seeing(child.pid(), &marker);
        let proc = sys.process(sysinfo::Pid::from_u32(child.pid())).expect("child");
        terminate_mcp_server(proc);
        let status = child.wait_exit().expect("child survived SIGTERM");
        {
            use std::os::unix::process::ExitStatusExt;
            assert_eq!(status.signal(), Some(15));
        }
        assert!(pid_is_alive(grandchild.0), "POSIX terminate must not walk the tree");
    }

    #[cfg(windows)]
    #[test]
    fn terminate_mcp_server_windows_kills_the_process_tree() {
        // V52-AI on Windows: the host AND its children (they hold the locks).
        use vct_launcher_core::process::pid_is_alive;
        let py = python_runtime_exe();
        let (mut child, grandchild, marker) = spawn_mcp_probe_with_grandchild(&py, "win");
        let sys = snapshot_seeing(child.pid(), &marker);
        let proc = sys.process(sysinfo::Pid::from_u32(child.pid())).expect("child");
        terminate_mcp_server(proc);
        assert!(child.wait_exit().is_some(), "child survived taskkill /T /F");
        let mut gone = false;
        for _ in 0..100 {
            if !pid_is_alive(grandchild.0) {
                gone = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        assert!(gone, "taskkill /T must take the MCP's child tree down too");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn snapshot_lists_processes_not_their_threads() {
        // `without_tasks()`: a thread must never be a sweep row of its own.
        let _held = std::thread::spawn(|| std::thread::sleep(std::time::Duration::from_secs(3)));
        let sys = process_table_with_cmdlines();
        assert!(
            sys.processes()
                .values()
                .all(|p| p.thread_kind() != Some(sysinfo::ThreadKind::Userland)),
            "the snapshot lists userland threads as processes"
        );
        let me = std::process::id();
        let tids: Vec<u32> = std::fs::read_dir("/proc/self/task")
            .expect("/proc/self/task")
            .filter_map(|e| e.ok()?.file_name().to_str()?.parse().ok())
            .filter(|tid| *tid != me)
            .collect();
        assert!(!tids.is_empty(), "the test process should have more than one thread");
        for tid in tids {
            assert!(
                sys.process(sysinfo::Pid::from_u32(tid)).is_none(),
                "thread {tid} of the test process is listed as a process"
            );
        }
    }

    #[test]
    fn orphan_reaper_sees_child_cmdline_and_leaves_live_parented_mcp_alone() {
        let me = std::process::id();
        let py = python_runtime_exe();
        let marker = mcp_script_marker("reap");
        let child = spawn_mcp_probe(&py, &marker, &[]);
        let sys = snapshot_seeing(child.pid(), &marker);

        let pid = sysinfo::Pid::from_u32(child.pid());
        let proc = sys.process(pid).expect("child present in the snapshot");
        let inputs = reap_inputs(&sys, pid, proc, me);
        assert!(
            inputs.cmdline.contains(&marker),
            "the reaper read an empty cmdline for a live MCP child: {inputs:?}"
        );
        assert!(is_mcp_server_argv(&inputs.argv));
        assert_eq!(inputs.parent_pid, Some(me));
        assert_eq!(inputs.parent_alive, Some(true));
        assert!(!inputs.parent_is_subreaper);
        // An MCP, but its parent (us) is alive: leave it alone.
        assert!(!inputs.should_reap());
        assert_eq!(inputs.orphan_signal(), None);

        let mut targeted = Vec::new();
        let _ = run_orphaned_mcp_reap(&sys, me, &mut |p| targeted.push(p.pid().as_u32()));
        assert!(!targeted.contains(&child.pid()));
    }

    #[test]
    fn orphan_signal_names_the_reason_that_fired() {
        // Signal (a): known non-subreaper parent dead.
        let dead = steady_state_orphan_signal(false, COORD_CMD, 3600, Some(4242), Some(false), false);
        assert_eq!(dead, Some(OrphanSignal::ParentDead));
        assert_eq!(dead.unwrap().describe(Some(4242)), "parent PID 4242 is dead");
        // Signal (b): reparented to a LIVE subreaper — must not say "dead".
        let reparented = steady_state_orphan_signal(false, COORD_CMD, 3600, Some(1), Some(true), true);
        assert_eq!(reparented, Some(OrphanSignal::ReparentedToSubreaper));
        let text = reparented.unwrap().describe(Some(1));
        assert_eq!(text, "reparented to subreaper PID 1");
        assert!(!text.contains("dead"));
        // Leave-alone: live real parent.
        assert_eq!(
            steady_state_orphan_signal(false, COORD_CMD, 3600, Some(4242), Some(true), false),
            None
        );
    }

    #[test]
    fn hub_sweep_enumeration_sees_hub_decoy_cmdline() {
        // A decoy whose EXE BASENAME is a hub binary.
        let decoy = linked_test_exe(if cfg!(windows) { "vct-hub.exe" } else { "vct-hub" });
        let marker = non_mcp_marker("hub");
        let child = spawn_probe_from(&decoy.path, &marker, &[]);
        let sys = snapshot_seeing(child.pid(), &marker);

        let candidates = hub_sweep_candidates(&sys, std::process::id());
        let hit = candidates
            .iter()
            .find(|c| c.pid == child.pid())
            .expect("hub sweep misses a live process whose exe basename is vct-hub");
        assert!(hub_exe_basename_match(&hit.exe_basename));
        assert!(
            hit.cmdline.contains(&marker),
            "hub sweep candidate carries no cmdline: {hit:?}"
        );
    }

    #[test]
    fn process_exe_by_pid_resolves_spawned_child() {
        let marker = non_mcp_marker("exe");
        let child = spawn_probe(&marker);
        let _ = snapshot_seeing(child.pid(), &marker);
        let got = process_exe_by_pid(child.pid()).expect("child exe resolves");
        let want = std::env::current_exe().expect("current_exe");
        assert_eq!(
            std::fs::canonicalize(&got).unwrap_or(got),
            std::fs::canonicalize(&want).unwrap_or(want)
        );
    }
}
