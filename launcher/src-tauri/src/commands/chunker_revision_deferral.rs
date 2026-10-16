// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Chunker-REVISION deferral hook (v0.2.92 R2-4).
//!
//! On every boot the launcher compares the live `_CHUNKER_REVISION`
//! sentinel from `claude_mcp_servers/weaviate_mcp/chunking.py` against the
//! persisted last-seen value (`app_state` key `chunker.last_seen_revision`);
//! on change it appends an entry to the orchestrator-root project's
//! `<project>/.claude/context/UPDATE_DEFERRED.md` prompting the user to
//! re-sync KG / codegraph so existing rows are re-chunked under the new
//! revision.
//!
//! Why this matters: when chunk boundaries change, existing Weaviate rows
//! synced under the older revision have stale boundaries. Search recall
//! degrades on long answers because relevant content lives in chunk N+1
//! that the new revision would have folded into chunk N. Re-syncing
//! rebuilds the chunks and restores recall.
//!
//! (History: the original v0.2.47 hook keyed on the one-off v0.2.46
//! semver boundary — `CHUNKER_BUMP_VERSION` / `write_chunker_deferral_if_crossing_boundary`.
//! It was retired in v0.2.103, owner decision 2026-10-15, as superseded by
//! this revision-keyed path plus the per-project state-keyed gate in
//! `vco_lib.chunker_revision`, which fire regardless of how many releases
//! were skipped.)
//!
//! Soft-fail discipline: the hook can fail in any step (filesystem
//! errors, project unreachable, python unavailable) without blocking
//! launcher startup. Worst case: the user keeps the stale chunks and
//! search quality slowly degrades — but the launcher still boots and the
//! user can re-sync manually later.

use crate::db::Db;
use std::path::PathBuf;
use vct_launcher_core::process::CommandExt as _;

/// app_state key persisting the last-seen `_CHUNKER_REVISION` sentinel string
/// (R2-4). Compared against the live value read from chunking.py each boot; a
/// mismatch triggers the re-sync deferral. This is the REVISION-crossing check
/// that the sentinel's own contract comment always promised.
pub(crate) const APP_STATE_KEY_CHUNKER_REVISION: &str = "chunker.last_seen_revision";

/// Resolve a Python interpreter for the chunker-resync deferral `-c` snippet.
///
/// v0.2.77 (Part 7c task 1): delegates to the shared RT-4 ladder in
/// `vct_launcher_core::python_resolve`. Previously a PATH-only walk; the ladder
/// prefers the orchestrator venv (which has `vco_lib` importable) before PATH,
/// keeping every Rust-side emitter's resolution consistent (one home).
fn pick_python() -> Option<String> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib_str()
}

/// Quote `s` as a Python double-quoted string literal. Mirrors
/// `storage_ux::py_quote` byte-for-byte (kept local: that fn is private and
/// widening its visibility for one caller adds no architectural value — same
/// rationale documented in `module_updates::py_quote`).
fn py_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Outcome of the chunker-REVISION-change check (R2-4), keyed on the
/// `_CHUNKER_REVISION` sentinel STRING, not the launcher semver. Exposed for
/// tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RevisionOutcome {
    /// First boot with revision-tracking (no prior revision recorded). The key
    /// is seeded to the current revision; no deferral is written. Note the
    /// limitation: an install updating from a pre-revision-tracking version may
    /// hold rows written under an OLDER chunker revision, and this seeding
    /// cannot detect that (there is no prior marker to compare) — the
    /// per-project state-keyed gate in `vco_lib.chunker_revision` covers the
    /// bundle-update surfaces for that population, and any later revision
    /// bump is caught from the seeded marker onward.
    FirstBoot,
    /// The persisted revision matches the live one. No re-chunk needed.
    Unchanged,
    /// The revision changed; a re-sync deferral was written to the
    /// orchestrator-root project (`projects_touched` = 1 on success, 0 when no
    /// root row / folder missing / write failed). The marker is advanced so the
    /// deferral fires once per revision, not every boot.
    Changed {
        prev: String,
        current: String,
        projects_touched: usize,
    },
    /// The live revision could not be read (python/repo-root unavailable) or a
    /// DB read failed. Logged on stderr; treated as a no-op (next boot retries).
    Skipped,
}

/// Read the live `_CHUNKER_REVISION` sentinel from `chunking.py` via the shared
/// Python reader (`vco_lib.project_init.current_chunker_revision`). ONE reader
/// so the launcher and Python agree on the value (A>B>C cross-language: shared
/// code via subprocess). Returns `None` when python/repo-root is unavailable or
/// the helper errors — the caller then Skips.
fn read_current_chunker_revision() -> Option<String> {
    let repo_root = super::installer::find_local_repo_root().ok()?;
    let py = pick_python()?;
    let repo_py = py_quote(&repo_root.to_string_lossy());
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from vco_lib.project_init import current_chunker_revision\n\
         sys.stdout.write(current_chunker_revision())\n",
    );
    let output = std::process::Command::new(&py)
        .silent()
        .arg("-c")
        .arg(&script)
        .output()
        .ok()?;
    if !output.status.success() {
        tracing::warn!(
            "[chunker-revision] reader exited {} — skipping",
            output.status
        );
        return None;
    }
    let rev = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if rev.is_empty() {
        tracing::warn!("[chunker-revision] reader returned empty string — skipping");
        return None;
    }
    Some(rev)
}

/// Write the REVISION-change re-sync deferral to the orchestrator-root project.
/// Resolves the orchestrator-root row from `launcher.db` and routes to the
/// revision-keyed Python emitter. Returns 1 on success, 0 otherwise.
/// Soft-fails throughout.
fn write_revision_deferral_for_root_project(db: &Db, prev: &str, current: &str) -> usize {
    let guard = db.lock();
    let folder_path_result: Result<String, rusqlite::Error> = guard.query_row(
        "SELECT folder_path FROM projects WHERE host = 'orchestrator_root' LIMIT 1",
        [],
        |row| row.get(0),
    );
    drop(guard);

    let folder_str = match folder_path_result {
        Ok(s) => s,
        Err(rusqlite::Error::QueryReturnedNoRows) => return 0,
        Err(e) => {
            tracing::warn!(
                "[chunker-revision] SELECT orchestrator-root failed: {} — skipping",
                e
            );
            return 0;
        }
    };
    let folder = PathBuf::from(&folder_str);
    if !folder.is_dir() {
        tracing::warn!(
            "[chunker-revision] orchestrator-root folder missing on disk: {} — skipping",
            folder_str
        );
        return 0;
    }

    let repo_root = match super::installer::find_local_repo_root() {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("[chunker-revision] cannot locate repo root: {} — skipping", e);
            return 0;
        }
    };
    let py = match pick_python() {
        Some(p) => p,
        None => {
            tracing::warn!("[chunker-revision] no python to emit deferral — skipping");
            return 0;
        }
    };
    let repo_py = py_quote(&repo_root.to_string_lossy());
    let folder_py = py_quote(&folder.to_string_lossy());
    let prev_py = py_quote(prev);
    let cur_py = py_quote(current);
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.project_init import _emit_chunker_revision_resync_deferral\n\
         _emit_chunker_revision_resync_deferral(Path({folder_py}), {prev_py}, {cur_py})\n",
    );
    let status = std::process::Command::new(&py)
        .silent()
        .arg("-c")
        .arg(&script)
        .status();
    match status {
        Ok(s) if s.success() => 1,
        Ok(s) => {
            tracing::warn!("[chunker-revision] deferral python helper exited {}", s);
            0
        }
        Err(e) => {
            tracing::warn!("[chunker-revision] deferral python helper spawn failed: {}", e);
            0
        }
    }
}

/// Public entry point (R2-4): fire the re-sync deferral when the live
/// `_CHUNKER_REVISION` sentinel differs from the persisted last-seen value.
///
/// Called from `lib.rs::setup` on every boot. This is the consumer the
/// sentinel's contract comment always described: bump `_CHUNKER_REVISION` in
/// chunking.py whenever chunk boundaries change → the next boot detects the
/// change → writes the re-sync deferral with the kg-sync/code-graph re-run
/// commands → advances the marker so it fires exactly once per revision.
///
/// Soft-fails throughout: a read/DB/python failure Skips (no crash, next boot
/// retries). First boot seeds the marker WITHOUT a deferral (rows are current
/// by definition on a fresh revision-tracking install).
pub fn write_chunker_deferral_if_revision_changed(db: &Db) -> RevisionOutcome {
    let current = match read_current_chunker_revision() {
        Some(r) => r,
        None => return RevisionOutcome::Skipped,
    };
    let prior = match db.app_state_get(APP_STATE_KEY_CHUNKER_REVISION) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("[chunker-revision] app_state_get failed: {} — skipping", e);
            return RevisionOutcome::Skipped;
        }
    };
    match prior.as_deref() {
        None => {
            // First boot with revision-tracking: seed WITHOUT a deferral. Rows
            // already on disk were chunked under whatever revision shipped with
            // this build, so there's nothing older to re-chunk against.
            if let Err(e) = db.app_state_set(APP_STATE_KEY_CHUNKER_REVISION, &current) {
                tracing::warn!("[chunker-revision] seed failed: {} — next boot retries", e);
            }
            RevisionOutcome::FirstBoot
        }
        Some(prev) if prev == current => RevisionOutcome::Unchanged,
        Some(prev) => {
            let prev = prev.to_string();
            let touched = write_revision_deferral_for_root_project(db, &prev, &current);
            // Advance the marker so the deferral fires once per revision, not
            // every boot. Soft-fail: if this fails the deferral re-emits next
            // boot but DeferralReport dedups by condition_id (no duplicate).
            if let Err(e) = db.app_state_set(APP_STATE_KEY_CHUNKER_REVISION, &current) {
                tracing::warn!(
                    "[chunker-revision] marker advance failed: {} — deferral may re-emit (dedup'd)",
                    e
                );
            }
            tracing::info!(
                "[chunker-revision] revision changed {} → {}; wrote re-sync notice \
                 to orchestrator-root project ({} written)",
                prev, current, touched
            );
            RevisionOutcome::Changed {
                prev,
                current,
                projects_touched: touched,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The retired semver-boundary half (`CHUNKER_BUMP_VERSION`,
    // `compare_versions`, `write_chunker_deferral_if_crossing_boundary`) was
    // removed in v0.2.103 (owner decision 2026-10-15) together with its tests;
    // the Python emitter's own content is pinned by
    // `tests/test_deferral_command_argparse_sweep.py` and
    // `tests/test_v0292_codegraph_force_drop_guard.py`, which run the LIVE
    // revision-keyed emitter.

    #[test]
    fn revision_first_boot_seeds_marker_without_deferral() {
        // No prior revision → FirstBoot (marker seeded, no re-chunk needed).
        // If python/repo-root is unavailable the reader returns None → Skipped;
        // accept either since the routing (not the reader) is under test here.
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_revision_changed(&db);
        match outcome {
            RevisionOutcome::FirstBoot => {
                // Marker must now be set to the live revision.
                let seeded = db
                    .app_state_get(APP_STATE_KEY_CHUNKER_REVISION)
                    .expect("app_state_get")
                    .expect("marker seeded on first boot");
                assert!(!seeded.is_empty(), "seeded revision must be non-empty");
            }
            RevisionOutcome::Skipped => {
                eprintln!("skipping content assertion: revision reader unavailable");
            }
            other => panic!("expected FirstBoot or Skipped, got {:?}", other),
        }
    }

    #[test]
    fn revision_unchanged_is_noop() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Seed the marker to whatever the live reader returns (first call).
        let first = write_chunker_deferral_if_revision_changed(&db);
        if matches!(first, RevisionOutcome::Skipped) {
            eprintln!("skipping: revision reader unavailable");
            return;
        }
        // Second call with the SAME live revision → Unchanged.
        let second = write_chunker_deferral_if_revision_changed(&db);
        assert_eq!(second, RevisionOutcome::Unchanged);
    }

    #[test]
    fn revision_change_advances_marker_and_reports_changed() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Force a STALE persisted revision so the live value (whatever it is)
        // differs → Changed. No orchestrator-root row exists, so touched == 0,
        // but the marker must still advance to the live value.
        db.app_state_set(APP_STATE_KEY_CHUNKER_REVISION, "v0.0.0-stale")
            .expect("seed stale marker");
        let outcome = write_chunker_deferral_if_revision_changed(&db);
        match outcome {
            RevisionOutcome::Changed {
                prev,
                current,
                projects_touched,
            } => {
                assert_eq!(prev, "v0.0.0-stale");
                assert_ne!(current, "v0.0.0-stale", "current must be the live revision");
                assert_eq!(projects_touched, 0, "no orchestrator-root row in in-memory db");
                // Marker advanced to the live value → next boot is Unchanged.
                let advanced = db
                    .app_state_get(APP_STATE_KEY_CHUNKER_REVISION)
                    .expect("app_state_get")
                    .expect("marker present");
                assert_eq!(advanced, current, "marker must advance to the live revision");
            }
            RevisionOutcome::Skipped => {
                eprintln!("skipping: revision reader unavailable");
            }
            other => panic!("expected Changed or Skipped, got {:?}", other),
        }
    }
}
