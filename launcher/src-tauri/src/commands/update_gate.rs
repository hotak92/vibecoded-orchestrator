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
//! 1. `update_orchestrator` writes the lockfile pre-git-pull via
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
/// WHY: `update_orchestrator` closes the launcher's managed `Db`
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
/// Designed to be held for the entire `update_orchestrator` body. Any
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

// ─── Pre-update MCP kill sweep ───────────────────────────────────────────
//
// Before the launcher writes the lockfile + starts git pull, do a
// best-effort sweep of currently-running MCP processes that could be
// holding file locks on the binaries we're about to overwrite. This
// is the *prevention* side of the gate; the lockfile itself is the
// *suppression* side.
//
// CRITICAL: this sweep MUST NOT fire outside `update_orchestrator`. We
// don't want to kill the user's other Python or Node processes.
// The filter pattern is strict — only commands matching:
//   * `claude_mcp_servers/` (path substring) — our own MCPs
//   * `@modelcontextprotocol/` — npx-spawned MCP packages
//   * `@upstash/context7-mcp` — the specific MCP the user reported
//
// On Windows the sweep uses taskkill with /FI filters. On POSIX it
// enumerates /proc and sends SIGTERM. Either way, failure is soft —
// logged but doesn't block the update (the lockfile + MCP-side gate
// catches whatever the sweep missed).

const MCP_PATTERN_SUBSTRINGS: &[&str] = &[
    "claude_mcp_servers",
    "@modelcontextprotocol",
    "@upstash/context7",
];

/// Run a pre-update MCP kill sweep. Best-effort, soft-fail.
///
/// Returns the number of processes that were targeted (informational
/// only — even if 0, the gate still protects future respawns).
pub fn pre_update_mcp_kill_sweep() -> usize {
    #[cfg(target_os = "windows")]
    {
        pre_update_mcp_kill_sweep_windows()
    }
    #[cfg(not(target_os = "windows"))]
    {
        pre_update_mcp_kill_sweep_posix()
    }
}

#[cfg(not(target_os = "windows"))]
fn pre_update_mcp_kill_sweep_posix() -> usize {
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);
    let my_pid = std::process::id();
    let mut count = 0usize;
    for (pid, proc) in sys.processes() {
        // Never kill ourselves or our parent (the launcher).
        if pid.as_u32() == my_pid {
            continue;
        }
        let cmdline = proc
            .cmd()
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
        if !mcp_pattern_match(&cmdline) {
            continue;
        }
        tracing::info!(
            "[update_gate] kill-sweep: terminating PID {} (cmd: {})",
            pid.as_u32(),
            cmdline.chars().take(120).collect::<String>()
        );
        // SIGTERM first; we don't wait for SIGKILL in this best-effort
        // sweep. The lockfile gate prevents respawns.
        proc.kill_with(sysinfo::Signal::Term);
        count += 1;
    }
    count
}

#[cfg(target_os = "windows")]
fn pre_update_mcp_kill_sweep_windows() -> usize {
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);
    let my_pid = std::process::id();
    let mut count = 0usize;
    for (pid, proc) in sys.processes() {
        if pid.as_u32() == my_pid {
            continue;
        }
        let cmdline = proc
            .cmd()
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
        if !mcp_pattern_match(&cmdline) {
            continue;
        }
        tracing::info!(
            "[update_gate] kill-sweep: terminating PID {} (cmd: {})",
            pid.as_u32(),
            cmdline.chars().take(120).collect::<String>()
        );
        proc.kill();
        count += 1;
    }
    count
}

/// Return true if `cmdline` matches an MCP pattern we should sweep.
///
/// Pure helper, isolated for unit testing — process enumeration is
/// hard to mock but the pattern logic is deterministic.
pub fn mcp_pattern_match(cmdline: &str) -> bool {
    MCP_PATTERN_SUBSTRINGS
        .iter()
        .any(|pat| cmdline.contains(pat))
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
/// the MCP-pattern + age gates, EITHER orphan signal qualifies:
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
pub fn steady_state_mcp_should_reap(
    is_self: bool,
    cmdline: &str,
    run_time_secs: u64,
    parent_pid: Option<u32>,
    parent_alive: Option<bool>,
    parent_is_subreaper: bool,
) -> bool {
    if is_self {
        return false;
    }
    if !mcp_pattern_match(cmdline) {
        return false;
    }
    if run_time_secs < STEADY_STATE_MCP_MIN_AGE_SECS {
        return false;
    }
    // No known parent → can't positively confirm orphanhood → leave alone.
    if parent_pid.is_none() {
        return false;
    }
    // Orphan signal (b): reparented to a subreaper (the POSIX orphan case —
    // a healthy session-attached MCP is never parented to a subreaper).
    if parent_is_subreaper {
        return true;
    }
    // Orphan signal (a): we definitively know the (non-subreaper) parent is dead
    // (the Windows case — Windows does not reparent orphans).
    matches!(parent_alive, Some(false))
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
/// ([`steady_state_mcp_should_reap`]) enforces that.
///
/// Posture: SIGTERM (graceful) + soft-fail, never SIGKILL, never blocks boot.
/// Returns the number of orphans signalled (informational).
pub fn steady_state_orphaned_mcp_reap() -> usize {
    use sysinfo::System;
    use vct_launcher_core::process::pid_is_alive;

    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);
    let my_pid = std::process::id();
    let mut count = 0usize;

    for (pid, proc) in sys.processes() {
        let is_self = pid.as_u32() == my_pid;
        let cmdline = proc
            .cmd()
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
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

        if !steady_state_mcp_should_reap(
            is_self,
            &cmdline,
            proc.run_time(),
            parent_pid,
            parent_alive,
            parent_is_subreaper,
        ) {
            continue;
        }

        tracing::warn!(
            "[update_gate] steady-state reap: terminating ORPHANED MCP PID {} \
             (parent {:?} dead, age {}s, cmd: {})",
            pid.as_u32(),
            parent_pid,
            proc.run_time(),
            cmdline.chars().take(120).collect::<String>()
        );
        // SIGTERM (graceful) + soft-fail, mirroring pre_update_mcp_kill_sweep.
        match proc.kill_with(sysinfo::Signal::Term) {
            Some(_) => {}
            None => {
                proc.kill();
            }
        }
        count += 1;
    }
    count
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
/// Any such hub keeps `~/.vct/launcher.db` open (blocking DB writes)
/// and, on Windows, keeps `vct-hub.exe` locked (blocking the binary
/// swap). This sweep is the process-identity backstop: after the
/// lockfile-driven stop, terminate any REMAINING hub binary.
///
/// Posture (per the 2026-06-15 decision): SIGTERM (graceful) + soft-fail
/// — mirrors `pre_update_mcp_kill_sweep`. We never SIGKILL here and we
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
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);
    let my_pid = std::process::id();
    let mut count = 0usize;
    for (pid, proc) in sys.processes() {
        // Never signal ourselves (the launcher) or our parent.
        if pid.as_u32() == my_pid {
            continue;
        }
        // Identify by executable basename, not cmdline substring.
        let exe_basename = proc
            .exe()
            .and_then(|p| p.file_name())
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        if !hub_exe_basename_match(&exe_basename) {
            continue;
        }
        let cmdline = proc
            .cmd()
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
        tracing::info!(
            "[update_gate] hub-sweep: terminating stray vct-hub PID {} (exe: {}, cmd: {})",
            pid.as_u32(),
            exe_basename,
            cmdline.chars().take(120).collect::<String>()
        );
        // SIGTERM (graceful) + soft-fail. `kill_with` returns None when
        // the platform lacks the signal — fall back to the default
        // terminate in that case. We do NOT escalate to SIGKILL or wait.
        match proc.kill_with(sysinfo::Signal::Term) {
            Some(_) => {}
            None => {
                proc.kill();
            }
        }
        count += 1;
    }
    count
}

/// v0.2.63: resolve a running process's executable PATH by pid, via the same
/// sysinfo backend `pre_update_hub_kill_sweep` uses. Returns `None` if the pid
/// is gone or the platform won't expose the exe path (permissions, etc.).
///
/// Used by the launcher's boot-time hub-identity check
/// ([`crate::hub_launcher::ensure_hub_running`]) to decide whether a running
/// hub is the launcher's install-folder copy or a foreign/stale binary (a dev
/// `cargo run`, a different checkout, an old install) that must be swapped.
/// Callers MUST treat `None` as "can't tell — leave it alone" (no false kills).
pub fn process_exe_by_pid(pid: u32) -> Option<std::path::PathBuf> {
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);
    // Mirror pre_update_hub_kill_sweep's iteration shape rather than the
    // single-pid refresh API, so behaviour stays identical across sysinfo
    // versions already validated by the sweep.
    for (p, proc) in sys.processes() {
        if p.as_u32() == pid {
            return proc.exe().map(|e| e.to_path_buf());
        }
    }
    None
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

    // ── mcp_pattern_match ───────────────────────────────────────────────

    #[test]
    fn pattern_matches_claude_mcp_servers() {
        assert!(mcp_pattern_match(
            "/home/x/.venv/bin/python /home/x/claude_mcp_servers/weaviate_mcp/server.py"
        ));
    }

    #[test]
    fn pattern_matches_modelcontextprotocol() {
        assert!(mcp_pattern_match(
            "node /usr/lib/node_modules/@modelcontextprotocol/server-everything/dist/index.js"
        ));
    }

    #[test]
    fn pattern_matches_upstash_context7() {
        assert!(mcp_pattern_match(
            "npx -y @upstash/context7-mcp@latest"
        ));
    }

    #[test]
    fn pattern_does_not_match_unrelated_python() {
        // User's other Python projects MUST survive the sweep.
        assert!(!mcp_pattern_match(
            "/usr/bin/python3 /home/user/myproject/main.py --port 8000"
        ));
    }

    #[test]
    fn pattern_does_not_match_unrelated_node() {
        assert!(!mcp_pattern_match(
            "node /home/user/another-app/server.js"
        ));
    }

    #[test]
    fn pattern_does_not_match_launcher_itself() {
        assert!(!mcp_pattern_match(
            "/home/x/.vct/dist/vct-launcher"
        ));
    }

    #[test]
    fn pattern_does_not_match_hub_itself() {
        assert!(!mcp_pattern_match(
            "/home/x/.vct/dist/vct-hub --start-if-not-running"
        ));
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

    const COORD_CMD: &str =
        "/x/.venv/bin/python /x/claude_mcp_servers/vct_coordination_mcp/server.py";

    #[test]
    fn reap_when_parent_dead_and_old_enough() {
        // Signal (a) — dead parent (the Windows case; POSIX reparents so this is
        // rare there). Matches MCP pattern, past the grace window, parent dead → REAP.
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
            "/usr/bin/python3 /home/user/myproject/main.py", // not an MCP
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
            "/usr/bin/python3 /home/user/myproject/main.py",
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
    // The runner enumerates the live process table via sysinfo and applies the
    // predicate above. We can't inject a fake process table at this seam, but the
    // contract that matters for boot safety is: it NEVER panics and is a no-op on
    // a healthy system (no orphaned MCPs). The test environment has no MCP whose
    // parent is dead, so the sweep must complete and return a count without
    // touching any process. This proves the best-effort/soft-fail posture
    // end-to-end (no `.unwrap()`/`.expect()` on the sysinfo path, graceful
    // `kill_with` fallback) — exactly what the startup-block call relies on.

    #[test]
    fn reaper_runner_is_no_panic_and_returns_count() {
        // Must not panic. On a healthy install (no orphaned MCPs) this is 0;
        // we only assert it returns a usize without unwinding — the boot path
        // must never crash the launcher even if sysinfo behaves oddly.
        let reaped = steady_state_orphaned_mcp_reap();
        // The pid-sanity / parent-alive guards mean a sane sandbox yields 0;
        // assert the count is a plausible non-pathological value rather than
        // pinning it to exactly 0 (a CI runner could conceivably have a real
        // orphan). `usize` is always >= 0; this documents the no-op intent.
        assert!(
            reaped == 0 || reaped < 100_000,
            "reaper returned an implausible count ({reaped}) — likely a runaway match"
        );
    }

    #[test]
    fn reaper_runner_never_reaps_own_process() {
        // Calling the reaper from the test binary must not signal the test
        // process itself (is_self short-circuit + the launcher cmdline never
        // matches an MCP pattern). If it did, the test harness would die mid-run
        // rather than completing — reaching the assertion proves we survived.
        let _ = steady_state_orphaned_mcp_reap();
        // Reaching here means the test process was not SIGTERM'd by its own call.
        assert!(pid_alive_self_sanity());
    }

    /// Local helper: confirm our own pid still reports alive after the reaper
    /// ran (re-uses the same liveness probe the runner uses for parents).
    fn pid_alive_self_sanity() -> bool {
        vct_launcher_core::process::pid_is_alive(std::process::id())
    }
}
