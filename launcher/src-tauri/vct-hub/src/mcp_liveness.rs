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
//! loop runs. `<pid>` is the server's own interpreter pid; on Windows that
//! is the CHILD of the venv `python.exe` stub the client started, not the
//! stub (`vct_launcher_core::mcp_heartbeat`). This sweep starts from the
//! files and reads the pid from the record (`McpRecord::pid`), so every
//! check and the terminate act on the interpreter that wrote it — never on
//! a pid guessed from the client's side. This module runs inside the infra watchdog's tick (one
//! scheduler, no second loop) and, per file:
//!
//! * process gone → delete the file;
//! * pid reused (start token or cmdline does not match) → delete the file,
//!   never signal;
//! * identity cannot be told (unreadable token or cmdline) → leave it;
//! * heartbeat fresh → leave it;
//! * heartbeat older than [`STALE_SECS`], still with the same mtime at a
//!   tick at least [`CONFIRM_SECS`] after the tick that first found it stale,
//!   identity matching → end the process (SIGTERM, then a hard kill after
//!   [`TERM_GRACE`]; `TerminateProcess` on Windows), log one line, delete the
//!   file.
//!
//! The confirmation is what keeps a laptop resume from ending healthy
//! servers: after a suspend every heartbeat looks hours old until its
//! server's next 30 s beat, and the hub's first tick may come first. The
//! confirmation is TIME-based, not a tick count, so it holds whatever the
//! watchdog interval (`VCT_HUB_INFRA_WATCHDOG_INTERVAL_SECS` may be as low as
//! 5 s): [`CONFIRM_SECS`] is two and a half beats, so a live server has
//! refreshed its mtime before any tick may confirm (wave-2 check L-5).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::Deserialize;
use vct_launcher_core::process::{
    pid_is_alive, process_cmdline, process_start_token, start_token_verdict,
    terminate_verified, StartTokenVerdict,
};

/// The stale bound and the heartbeat location live in ONE home shared with
/// the launcher's boot orphan reaper: `vct_launcher_core::mcp_heartbeat`.
pub use vct_launcher_core::mcp_heartbeat::{heartbeat_dir, STALE_SECS};

/// How long after the first stale observation of one mtime a tick may
/// confirm it: 2.5 x the server's 30 s beat
/// (`vco_lib/mcp_stdio_lifetime.py::HEARTBEAT_SECONDS`; MUST stay at least
/// twice that), so a server that resumed from a suspend has beaten by then.
pub const CONFIRM_SECS: u64 = 75;

/// A heartbeat file a tick found stale: the mtime it had then, and when that
/// tick ran. Kept across ticks by the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Suspect {
    pub mtime: SystemTime,
    pub since: SystemTime,
}

/// The caller's cross-tick memory of [`Suspect`]s, one per heartbeat file.
pub type Suspects = HashMap<PathBuf, Suspect>;

/// Between the graceful signal and the hard kill.
pub const TERM_GRACE: Duration = Duration::from_secs(5);

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
    /// Stale, not yet confirmed: remembered (from its first observation),
    /// confirmed or cleared by a later tick.
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

/// The decision (pure). `suspected_for` = seconds since a tick first found
/// this file stale with the SAME mtime (`None`: not suspected yet). Ending
/// the process needs [`CONFIRM_SECS`] of that, whatever the tick interval.
pub fn decide(
    alive: bool,
    identity: Identity,
    age_secs: u64,
    suspected_for: Option<u64>,
) -> Verdict {
    if !alive {
        return Verdict::DeleteRecord;
    }
    match identity {
        Identity::Reused => Verdict::DeleteRecord,
        Identity::Unknown => Verdict::Leave,
        Identity::Same if age_secs < STALE_SECS => Verdict::Leave,
        Identity::Same => match suspected_for {
            Some(secs) if secs >= CONFIRM_SECS => Verdict::Terminate,
            _ => Verdict::Suspect,
        },
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
/// tick first found it stale, and that tick's time; it lives across ticks in
/// the caller. Returns how many servers were ended. Soft-fails throughout.
pub fn sweep(
    dir: &Path,
    suspects: &mut Suspects,
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
        let age_secs = vct_launcher_core::mcp_heartbeat::age_secs(mtime, now);
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
        let prior = suspects.get(&path).filter(|s| s.mtime == mtime).copied();
        let suspected_for = prior.map(|s| now.duration_since(s.since).map(|d| d.as_secs()).unwrap_or(0));
        match decide(alive, identity, age_secs, suspected_for) {
            Verdict::Leave => {
                suspects.remove(&path);
            }
            Verdict::Suspect => {
                // The FIRST observation of this mtime is kept: the window
                // runs from it, not from the latest tick.
                suspects.insert(path.clone(), prior.unwrap_or(Suspect { mtime, since: now }));
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
pub async fn sweep_tick(suspects: Suspects) -> Suspects {
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
    fn stale_and_matching_for_the_confirm_window_is_terminated() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS, Some(CONFIRM_SECS)), Verdict::Terminate);
        assert_eq!(decide(true, Identity::Same, STALE_SECS, Some(CONFIRM_SECS * 40)), Verdict::Terminate);
    }

    #[test]
    fn stale_for_the_first_time_is_only_suspected() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS + 9_999, None), Verdict::Suspect);
    }

    #[test]
    fn a_confirmation_inside_the_window_is_not_enough_whatever_the_tick_count() {
        // Wave-2 check L-5: a 5 s watchdog interval reaches its second (and
        // tenth) tick long before a resumed server's next 30 s beat.
        for secs in [0, 5, 10, 20, 30, CONFIRM_SECS - 1] {
            assert_eq!(decide(true, Identity::Same, STALE_SECS * 100, Some(secs)), Verdict::Suspect);
        }
    }

    #[test]
    fn the_window_covers_two_heartbeats() {
        // MUST stay >= 2 x vco_lib/mcp_stdio_lifetime.py HEARTBEAT_SECONDS (30).
        const { assert!(CONFIRM_SECS >= 60) };
    }

    #[test]
    fn a_fresh_heartbeat_is_left_alone() {
        assert_eq!(decide(true, Identity::Same, STALE_SECS - 1, Some(CONFIRM_SECS)), Verdict::Leave);
        assert_eq!(decide(true, Identity::Same, 0, None), Verdict::Leave);
    }

    #[test]
    fn a_reused_pid_loses_its_record_and_is_never_signalled() {
        assert_eq!(decide(true, Identity::Reused, STALE_SECS * 10, Some(CONFIRM_SECS)), Verdict::DeleteRecord);
    }

    #[test]
    fn an_identity_that_cannot_be_told_is_left_alone() {
        assert_eq!(decide(true, Identity::Unknown, STALE_SECS * 10, Some(CONFIRM_SECS)), Verdict::Leave);
    }

    #[test]
    fn a_gone_process_loses_its_record() {
        assert_eq!(decide(false, Identity::Unknown, 0, None), Verdict::DeleteRecord);
        assert_eq!(decide(false, Identity::Same, STALE_SECS, Some(CONFIRM_SECS)), Verdict::DeleteRecord);
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
    fn sweep_ends_a_stale_server_only_once_the_confirm_window_has_passed() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4242, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = fake(true, "proc:9", SERVER);
        let mut s = Suspects::new();
        assert_eq!(sweep(d.path(), &mut s, later, &os), 0, "first stale tick: suspect only");
        assert!(p.exists() && os.killed.borrow().is_empty());
        // 5 s ticks (the interval floor): many ticks, still inside the window.
        for k in 1..(CONFIRM_SECS / 5) {
            let t = later + Duration::from_secs(5 * k);
            assert_eq!(sweep(d.path(), &mut s, t, &os), 0, "tick {k} is inside the window");
        }
        assert!(p.exists() && os.killed.borrow().is_empty());
        assert_eq!(sweep(d.path(), &mut s, later + Duration::from_secs(CONFIRM_SECS), &os), 1);
        assert_eq!(*os.killed.borrow(), vec![4242]);
        assert!(!p.exists(), "record removed after the kill");
    }

    #[test]
    fn sweep_forgets_a_suspect_whose_heartbeat_resumed() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4243, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = fake(true, "proc:9", SERVER);
        let mut s = Suspects::new();
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
        let mut s = Suspects::new();
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
        let mut s = Suspects::new();
        sweep(d.path(), &mut s, later, &os);
        sweep(d.path(), &mut s, later + Duration::from_secs(CONFIRM_SECS), &os);
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
        let mut s = Suspects::new();
        sweep(d.path(), &mut s, SystemTime::now(), &os);
        assert!(!p.exists() && os.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_leaves_an_unidentifiable_server_alone() {
        let d = tempfile::tempdir().unwrap();
        let p = write(d.path(), 4248, "proc:9");
        let later = SystemTime::now() + Duration::from_secs(STALE_SECS + 60);
        let os = Fake { alive: true, token: Some("proc:9".into()), cmd: None, killed: RefCell::new(vec![]) };
        let mut s = Suspects::new();
        sweep(d.path(), &mut s, later, &os);
        sweep(d.path(), &mut s, later + Duration::from_secs(CONFIRM_SECS), &os);
        assert!(p.exists() && os.killed.borrow().is_empty());
    }

    #[test]
    fn sweep_without_the_directory_is_a_no_op() {
        let mut s = Suspects::new();
        let os = fake(true, "proc:9", SERVER);
        assert_eq!(sweep(Path::new("/nonexistent/vct/run/mcp"), &mut s, SystemTime::now(), &os), 0);
    }
}
