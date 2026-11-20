// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Ends a VCO stdio MCP server that is alive but no longer serving (v0.2.107).
//!
//! Field case: a weaviate-kg server stayed alive 54 h after a SIGHUP env
//! reload, every thread parked and its stdin unread, while Claude Code's
//! `/mcp` still said "connected" — `/mcp` reports transport state (process
//! alive, pipes open), not liveness, so every KG call of that chat and its
//! subagents hung. The hub cannot respawn a stdio child that Claude Code
//! owns, but if the hung process ENDS, Claude Code sees EOF and starts a
//! fresh server on the next call.
//!
//! Each server keeps a heartbeat file `<vct_root>/run/mcp/<pid>.json`
//! (`vco_lib/mcp_stdio_lifetime.py::Heartbeat` — the writer and the home of
//! the record format) whose mtime it refreshes every 30 s while its event
//! loop runs. This module runs inside the infra watchdog's tick (one
//! scheduler, no second loop) and, per file:
//!
//! * process gone → delete the file;
//! * pid reused (start token or cmdline does not match) → delete the file,
//!   never signal;
//! * identity cannot be told (unreadable token or cmdline) → leave it;
//! * heartbeat fresh → leave it;
//! * heartbeat older than [`STALE_SECS`] on TWO consecutive ticks with the
//!   same mtime, identity matching → end the process (SIGTERM, then a hard
//!   kill after [`TERM_GRACE`]; `TerminateProcess` on Windows), log one line,
//!   delete the file.
//!
//! The two-tick confirmation is what keeps a laptop resume from ending
//! healthy servers: after a suspend every heartbeat looks hours old until
//! its server's next 30 s beat, and the hub's first tick may come first.
//! The next tick (45 s later by default) sees the refreshed mtime.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::Deserialize;
use vct_launcher_core::process::{
    pid_is_alive, process_cmdline, process_start_token, start_token_verdict,
    terminate_verified, StartTokenVerdict,
};

/// A heartbeat this old is stale: twenty missed 30 s beats. The Python side
/// keeps beating through long synchronous tool calls (its beat comes from a
/// thread that only checks that the loop is running), so this bound is
/// about a loop that stopped, not one that is busy.
pub const STALE_SECS: u64 = 600;

/// Between the graceful signal and the hard kill.
pub const TERM_GRACE: Duration = Duration::from_secs(5);

/// `<vct_root>/run/mcp`. MUST MATCH
/// `vco_lib/mcp_stdio_lifetime.py::heartbeat_dir`.
pub fn heartbeat_dir() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join("run").join("mcp")
}

/// One heartbeat file. MUST MATCH the record `Heartbeat._write_record`
/// writes in `vco_lib/mcp_stdio_lifetime.py`.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct McpRecord {
    pub pid: u32,
    /// `process_start_token` of the server at start; `null` when the server
    /// could not read its own (then the hub never acts on the record).
    pub start_token: Option<String>,
    pub server: String,
    /// The last two components of the server's script path, `/`-joined.
    pub marker: String,
    /// Epoch seconds.
    pub started_at: f64,
}

/// Is the live process at `record.pid` the server that wrote the record?
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Identity {
    Same,
    /// The pid now names another process: the record is an orphan.
    Reused,
    /// Cannot tell — never act on it.
    Unknown,
}

/// What one tick does with one record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Leave,
    /// Stale for the first time: remembered, confirmed (or cleared) next tick.
    Suspect,
    DeleteRecord,
    Terminate,
}

/// Does any argument name the server script? Separators are normalised, so
/// a Windows `C:\..\weaviate_mcp\server.py` matches `weaviate_mcp/server.py`.
pub fn cmdline_has_marker(args: &[String], marker: &str) -> bool {
    !marker.is_empty()
        && args
            .iter()
            .any(|a| a.replace('\\', "/").ends_with(marker))
}

/// Identity from the two reads (pure). A cmdline that does not name the
/// script is a different process even when the token cannot be compared.
pub fn identity_of(
    record: &McpRecord,
    current_token: Option<&str>,
    cmdline: Option<&[String]>,
) -> Identity {
    let token = start_token_verdict(record.start_token.as_deref(), current_token);
    if token == StartTokenVerdict::Reused {
        return Identity::Reused;
    }
    match cmdline {
        None => Identity::Unknown,
        Some(args) if !cmdline_has_marker(args, &record.marker) => Identity::Reused,
        Some(_) if token == StartTokenVerdict::Same => Identity::Same,
        Some(_) => Identity::Unknown,
    }
}

/// The decision (pure). `stale_last_tick` = the previous tick already found
/// this file stale with the same mtime.
pub fn decide(alive: bool, identity: Identity, age_secs: u64, stale_last_tick: bool) -> Verdict {
    if !alive {
        return Verdict::DeleteRecord;
    }
    match identity {
        Identity::Reused => Verdict::DeleteRecord,
        Identity::Unknown => Verdict::Leave,
        Identity::Same if age_secs < STALE_SECS => Verdict::Leave,
        Identity::Same if !stale_last_tick => Verdict::Suspect,
        Identity::Same => Verdict::Terminate,
    }
}

/// What the sweep needs from the OS — a seam so the sweep's bookkeeping is
/// testable without real processes.
pub trait ProcessProbe {
    fn alive(&self, pid: u32) -> bool;
    fn start_token(&self, pid: u32) -> Option<String>;
    fn cmdline(&self, pid: u32) -> Option<Vec<String>>;
    fn terminate(&self, pid: u32, start_token: &str) -> bool;
}

/// The real OS, through `vct_launcher_core::process`.
pub struct OsProbe;

impl ProcessProbe for OsProbe {
    fn alive(&self, pid: u32) -> bool {
        pid_is_alive(pid)
    }
    fn start_token(&self, pid: u32) -> Option<String> {
        process_start_token(pid)
    }
    fn cmdline(&self, pid: u32) -> Option<Vec<String>> {
        process_cmdline(pid)
    }
    fn terminate(&self, pid: u32, start_token: &str) -> bool {
        terminate_verified(pid, start_token, TERM_GRACE)
    }
}

/// One pass over `dir`. `suspects` maps a file to the mtime it had when a
/// tick first found it stale; it lives across ticks in the caller. Returns
/// how many servers were ended. Soft-fails throughout.
pub fn sweep(
    dir: &Path,
    suspects: &mut HashMap<PathBuf, SystemTime>,
    now: SystemTime,
    probe: &dyn ProcessProbe,
) -> usize {
    let Ok(entries) = std::fs::read_dir(dir) else {
        suspects.clear();
        return 0;
    };
    let mut ended = 0usize;
    let mut seen: Vec<PathBuf> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Ok(mtime) = entry.metadata().and_then(|m| m.modified()) else {
            continue;
        };
        let age_secs = now.duration_since(mtime).map(|d| d.as_secs()).unwrap_or(0);
        let record: Option<McpRecord> = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok());
        let Some(record) = record else {
            // Unreadable/garbled and long untouched: nothing will ever beat it.
            if age_secs >= STALE_SECS {
                let _ = std::fs::remove_file(&path);
            }
            continue;
        };
        seen.push(path.clone());
        let alive = record.pid != 0 && record.pid != std::process::id() && probe.alive(record.pid);
        let identity = if alive {
            let token = probe.start_token(record.pid);
            let cmd = probe.cmdline(record.pid);
            identity_of(&record, token.as_deref(), cmd.as_deref())
        } else {
            Identity::Unknown
        };
        let stale_last_tick = suspects.get(&path) == Some(&mtime);
        match decide(alive, identity, age_secs, stale_last_tick) {
            Verdict::Leave => {
                suspects.remove(&path);
            }
            Verdict::Suspect => {
                suspects.insert(path.clone(), mtime);
            }
            Verdict::DeleteRecord => {
                suspects.remove(&path);
                let _ = std::fs::remove_file(&path);
            }
            Verdict::Terminate => {
                suspects.remove(&path);
                // `Same` implies a recorded token (see `identity_of`).
                let token = record.start_token.as_deref().unwrap_or_default();
                let gone = probe.terminate(record.pid, token);
                let up = now
                    .duration_since(SystemTime::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64() - record.started_at)
                    .unwrap_or(0.0)
                    .max(0.0);
                tracing::warn!(
                    "[vct-hub] mcp liveness: ended hung MCP server {} (pid {}): heartbeat {} s \
                     old, up {:.0} s{} — Claude Code starts a fresh one on its next call",
                    record.server,
                    record.pid,
                    age_secs,
                    up,
                    if gone { "" } else { "; still present after the hard kill" }
                );
                let _ = std::fs::remove_file(&path);
                ended += 1;
            }
        }
    }
    suspects.retain(|p, _| seen.contains(p));
    ended
}

/// The infra watchdog's call: one sweep of [`heartbeat_dir`] off the async
/// runtime (it reads `/proc` and may wait out [`TERM_GRACE`]).
pub async fn sweep_tick(suspects: HashMap<PathBuf, SystemTime>) -> HashMap<PathBuf, SystemTime> {
    let joined = tokio::task::spawn_blocking(move || {
        let mut suspects = suspects;
        sweep(&heartbeat_dir(), &mut suspects, SystemTime::now(), &OsProbe);
        suspects
    })
    .await;
    match joined {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!("[vct-hub] mcp liveness: sweep task failed: {}", e);
            HashMap::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    fn rec(pid: u32, token: Option<&str>) -> McpRecord {
        McpRecord {
            pid,
            start_token: token.map(str::to_string),
            server: "weaviate-kg".into(),
            marker: "weaviate_mcp/server.py".into(),
            started_at: 0.0,
        }
    }

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    // ── decide: act and leave-alone ──

    #[test]
    fn stale_and_matching_on_two_ticks_is_terminated() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS, true), Verdict::Terminate);
    }

    #[test]
    fn stale_for_the_first_time_is_only_suspected() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS + 9_999, false), Verdict::Suspect);
    }

    #[test]
    fn a_fresh_heartbeat_is_left_alone() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS - 1, true), Verdict::Leave);
        assert_eq!(decide(true, Identity::Same, 0, false), Verdict::Leave);
    }

    #[test]
    fn a_reused_pid_loses_its_record_and_is_never_signalled() {
        assert_eq!(decide(true, Identity::Reused, STALE_SECS * 10, true), Verdict::DeleteRecord);
    }

    #[test]
    fn an_identity_that_cannot_be_told_is_left_alone() {
        assert_eq!(decide(true, Identity::Unknown, STALE_SECS * 10, true), Verdict::Leave);
    }

    #[test]
    fn a_gone_process_loses_its_record() {
        assert_eq!(decide(false, Identity::Unknown, 0, false), Verdict::DeleteRecord);
        assert_eq!(decide(false, Identity::Same, STALE_SECS, true), Verdict::DeleteRecord);
    }

    // ── identity_of ──

    #[test]
    fn identity_needs_token_and_cmdline_to_match() {
        let r = rec(42, Some("proc:7"));
        let cmd = args(&["/v/bin/python", "/x/claude_mcp_servers/weaviate_mcp/server.py"]);
        assert_eq!(identity_of(&r, Some("proc:7"), Some(&cmd)), Identity::Same);
        assert_eq!(identity_of(&r, Some("proc:8"), Some(&cmd)), Identity::Reused);
        let other = args(&["/usr/bin/sleep", "100"]);
        assert_eq!(identity_of(&r, Some("proc:7"), Some(&other)), Identity::Reused);
        assert_eq!(identity_of(&r, Some("proc:7"), None), Identity::Unknown);
        assert_eq!(identity_of(&r, None, Some(&cmd)), Identity::Unknown);
        assert_eq!(identity_of(&rec(42, None), Some("proc:7"), Some(&cmd)), Identity::Unknown);
    }

    #[test]
    fn a_windows_cmdline_matches_the_slash_marker() {
        let cmd = args(&[r"C:\p\.venv\Scripts\python.exe", r"C:\p\claude_mcp_servers\weaviate_mcp\server.py"]);
        assert!(cmdline_has_marker(&cmd, "weaviate_mcp/server.py"));
        assert!(!cmdline_has_marker(&cmd, ""));
    }

    // ── sweep bookkeeping, with a fake OS ──

    struct Fake {
        alive: bool,
        token: Option<String>,
        cmd: Option<Vec<String>>,
        killed: RefCell<Vec<u32>>,
    }

    impl ProcessProbe for Fake {
        fn alive(&self, _pid: u32) -> bool {
            self.alive
        }
        fn start_token(&self, _pid: u32) -> Option<String> {
            self.token.clone()
        }
        fn cmdline(&self, _pid: u32) -> Option<Vec<String>> {
            self.cmd.clone()
        }
        fn terminate(&self, pid: u32, _t: &str) -> bool {
            self.killed.borrow_mut().push(pid);
            true
        }
    }

    fn fake(alive: bool, token: &str, cmd: &[&str]) -> Fake {
        Fake {
            alive,
            token: Some(token.into()),
            cmd: Some(args(cmd)),
            killed: RefCell::new(Vec::new()),
        }
    }

    fn write(dir: &Path, pid: u32, token: &str) -> PathBuf {
        let p = dir.join(format!("{pid}.json"));
        let body = serde_json::json!({
            "pid": pid, "start_token": token, "server": "weaviate-kg",
            "marker": "weaviate_mcp/server.py", "started_at": 1.0
        });
        std::fs::write(&p, body.to_string()).unwrap();
        p
    }

    const SERVER: &[&str] = &["python", "/r/claude_mcp_servers/weaviate_mcp/server.py"];

    #[test]
    fn sweep_ends_a_stale_server_on_the_second_tick_only() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4242, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = fake(true, "proc:9", SERVER);
        let mut s = HashMap::new();
        assert_eq!(sweep(d.path(), &mut s, later, &os), 0, "first stale tick: suspect only");
        assert!(p.exists() && os.killed.borrow().is_empty());
        assert_eq!(sweep(d.path(), &mut s, later, &os), 1);
        assert_eq!(*os.killed.borrow(), vec![4242]);
        assert!(!p.exists(), "record removed after the kill");
    }

    #[test]
    fn sweep_forgets_a_suspect_whose_heartbeat_resumed() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4243, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = fake(true, "proc:9", SERVER);
        let mut s = HashMap::new();
        sweep(d.path(), &mut s, later, &os);
        // The server beats (a resume from suspend): new mtime.
        std::thread::sleep(Duration::from_millis(20));
        write(d.path(), 4243, "proc:9");
        assert_eq!(sweep(d.path(), &mut s, SystemTime::now(), &os), 0);
        assert!(p.exists() && os.killed.borrow().is_empty());
        assert!(s.is_empty());
    }

    #[test]
    fn sweep_leaves_a_fresh_server_alone() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4244, "proc:9");
        let os = fake(true, "proc:9", SERVER);
        let mut s = HashMap::new();
        for _ in 0..3 {
            assert_eq!(sweep(d.path(), &mut s, SystemTime::now(), &os), 0);
        }
        assert!(p.exists() && os.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_deletes_the_record_of_a_reused_pid_without_signalling() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4245, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = fake(true, "proc:10", SERVER);
        let mut s = HashMap::new();
        sweep(d.path(), &mut s, later, &os);
        sweep(d.path(), &mut s, later, &os);
        assert!(!p.exists() && os.killed.borrow().is_empty());
        let p2 = write(d.path(), 4246, "proc:9");
        let stranger = fake(true, "proc:9", &["/usr/bin/sleep", "60"]);
        sweep(d.path(), &mut s, later, &stranger);
        assert!(!p2.exists() && stranger.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_deletes_the_record_of_a_gone_process() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4247, "proc:9");
        let os = fake(false, "proc:9", SERVER);
        let mut s = HashMap::new();
        sweep(d.path(), &mut s, SystemTime::now(), &os);
        assert!(!p.exists() && os.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_leaves_an_unidentifiable_server_alone() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4248, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = Fake { alive: true, token: Some("proc:9".into()), cmd: None, killed: RefCell::new(vec![]) };
        let mut s = HashMap::new();
        sweep(d.path(), &mut s, later, &os);
        sweep(d.path(), &mut s, later, &os);
        assert!(p.exists() && os.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_without_the_directory_is_a_no_op() {
        let mut s = HashMap::new();
        let os = fake(true, "proc:9", SERVER);
        assert_eq!(sweep(Path::new("/nonexistent/vct/run/mcp"), &mut s, SystemTime::now(), &os), 0);
    }
}
