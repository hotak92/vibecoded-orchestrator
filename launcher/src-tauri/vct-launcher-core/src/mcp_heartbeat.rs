// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Where a VCO stdio MCP server's liveness heartbeat lives, and when it is
//! fresh — the one home for both readers (v0.2.108).
//!
//! The writer is `vco_lib/mcp_stdio_lifetime.py::Heartbeat`: while a server's
//! event loop runs it keeps `<vct_root>/run/mcp/<pid>.json` and touches its
//! mtime every 30 s. Two readers act on it:
//!
//! * the hub's watchdog (`vct-hub/src/mcp_liveness.rs`) ends a server whose
//!   file has gone stale;
//! * the launcher's boot orphan reaper
//!   (`src/commands/update_gate.rs::steady_state_orphaned_mcp_reap`) never
//!   ends a server whose file is fresh.
//!
//! **Which pid names the file.** `<pid>` is the pid of the Python
//! interpreter that runs the server (`os.getpid()` in the server), and the
//! record's `pid` field is the same number. On Windows that is NOT the pid
//! the client started: a venv's `python.exe` is a launcher stub that starts
//! the real interpreter as its CHILD, so the client's spawned pid (the stub)
//! has no file and its child does. A reader that starts from a process it
//! found some other way (a process-table walk, the client's spawned pid) has
//! to look at that process AND its descendants — see
//! [`fresh_heartbeat_in_tree`]. On POSIX the two pids are the same.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// A heartbeat this old is stale: twenty missed 30 s beats
/// (`vco_lib/mcp_stdio_lifetime.py::HEARTBEAT_SECONDS`). The Python side
/// keeps beating through long synchronous tool calls (its beat comes from a
/// thread that only checks that the loop is running), so this bound is about
/// a loop that stopped, not one that is busy.
pub const STALE_SECS: u64 = 600;

/// `<vct_root>/run/mcp`. MUST MATCH
/// `vco_lib/mcp_stdio_lifetime.py::heartbeat_dir`.
pub fn heartbeat_dir() -> PathBuf {
    crate::paths::vct_root_dir().join("run").join("mcp")
}

/// The heartbeat file of the server whose interpreter pid is `pid`.
pub fn heartbeat_path(dir: &Path, pid: u32) -> PathBuf {
    dir.join(format!("{pid}.json"))
}

/// How old a heartbeat with this `mtime` is at `now`, in whole seconds. An
/// mtime in the future (clock step) reads as age 0 — fresh, never stale.
pub fn age_secs(mtime: SystemTime, now: SystemTime) -> u64 {
    now.duration_since(mtime).map(|d| d.as_secs()).unwrap_or(0)
}

/// Is there a heartbeat file for `pid` in `dir` younger than [`STALE_SECS`]
/// at `now`? A missing or unreadable file is "not fresh".
pub fn heartbeat_is_fresh(dir: &Path, pid: u32, now: SystemTime) -> bool {
    std::fs::metadata(heartbeat_path(dir, pid))
        .and_then(|m| m.modified())
        .map(|mtime| age_secs(mtime, now) < STALE_SECS)
        .unwrap_or(false)
}

/// The first pid in the process tree rooted at `root` — `root` itself, then
/// its descendants, breadth first — that has a fresh heartbeat, if any.
///
/// `children` maps a pid to its direct children (from one process-table
/// snapshot). The tree walk is what makes the Windows case work: the process
/// a reader finds is often the venv launcher stub, and the heartbeat is
/// named after its child interpreter. Bounded by the snapshot (each pid is
/// visited once).
pub fn fresh_heartbeat_in_tree(
    root: u32,
    children: &HashMap<u32, Vec<u32>>,
    is_fresh: &dyn Fn(u32) -> bool,
) -> Option<u32> {
    let mut queue = std::collections::VecDeque::from([root]);
    let mut seen = std::collections::HashSet::new();
    while let Some(pid) = queue.pop_front() {
        if !seen.insert(pid) {
            continue;
        }
        if is_fresh(pid) {
            return Some(pid);
        }
        if let Some(kids) = children.get(&pid) {
            queue.extend(kids.iter().copied());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_recent_file_is_fresh_and_an_old_or_missing_one_is_not() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        std::fs::write(heartbeat_path(dir, 4242), b"{}").unwrap();
        let now = SystemTime::now();
        assert!(heartbeat_is_fresh(dir, 4242, now));
        assert!(heartbeat_is_fresh(
            dir,
            4242,
            now + Duration::from_secs(STALE_SECS - 5)
        ));
        assert!(!heartbeat_is_fresh(
            dir,
            4242,
            now + Duration::from_secs(STALE_SECS + 5)
        ));
        assert!(!heartbeat_is_fresh(dir, 4243, now), "no file is not fresh");
    }

    #[test]
    fn a_future_mtime_reads_as_fresh() {
        let now = SystemTime::now();
        assert_eq!(age_secs(now + Duration::from_secs(60), now), 0);
    }

    #[test]
    fn the_tree_walk_finds_the_windows_child_interpreter() {
        // stub 10 -> interpreter 11 (+ conhost 12); the file names 11.
        let children = HashMap::from([(10, vec![11, 12]), (11, vec![])]);
        assert_eq!(
            fresh_heartbeat_in_tree(10, &children, &|p| p == 11),
            Some(11)
        );
        assert_eq!(
            fresh_heartbeat_in_tree(10, &children, &|p| p == 10),
            Some(10)
        );
        assert_eq!(fresh_heartbeat_in_tree(10, &children, &|_| false), None);
        // A pid outside the tree never counts.
        assert_eq!(fresh_heartbeat_in_tree(10, &children, &|p| p == 99), None);
    }

    #[test]
    fn the_tree_walk_survives_a_cycle() {
        // A pid-reuse artefact in a snapshot must not loop forever.
        let children = HashMap::from([(1, vec![2]), (2, vec![1])]);
        assert_eq!(fresh_heartbeat_in_tree(1, &children, &|_| false), None);
    }
}
