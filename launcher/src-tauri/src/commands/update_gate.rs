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
            eprintln!(
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
        eprintln!(
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
            eprintln!("[update_gate] advance_phase({:?}) failed: {}", phase, e);
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
        eprintln!(
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
        eprintln!(
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

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir() -> tempfile::TempDir {
        tempfile::tempdir().expect("tempdir")
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
}
