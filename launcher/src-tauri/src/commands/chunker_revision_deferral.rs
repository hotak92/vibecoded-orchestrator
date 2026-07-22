// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.47 RL-7.5 (2026-06-04) — Chunker-revision deferral hook.
//!
//! When the launcher detects an upgrade across the v0.2.46 chunker
//! boundary (any pre-v0.2.46 → v0.2.46+), it appends an entry to each
//! registered project's `<project>/.claude/context/UPDATE_DEFERRED.md`
//! prompting the user to re-sync KG / codegraph against the new chunker
//! presets (see `claude_mcp_servers/weaviate_mcp/chunking.py`
//! ::_CHUNKER_REVISION).
//!
//! Why this matters: post-v0.2.46 the chunker uses MUCH larger chunks
//! for qwen3-embedding (target 9500 tokens vs. legacy 1000), and
//! existing Weaviate rows synced under the legacy preset have stale
//! boundaries. Search recall degrades on long answers because relevant
//! content lives in chunk N+1 that the new preset would have folded
//! into chunk N. Re-syncing rebuilds the chunks under the new preset
//! and restores recall.
//!
//! Soft-fail discipline: the hook can fail in any step (filesystem
//! errors, project unreachable, malformed version string) without
//! blocking launcher startup. Worst case: the user keeps the legacy
//! chunks and search quality slowly degrades — but the launcher still
//! boots and the user can re-sync manually later.

use crate::db::Db;
use std::path::{Path, PathBuf};
use vct_launcher_core::process::CommandExt as _;

/// Version (inclusive lower bound on the NEW side) at which the
/// chunker-preset overhaul lands. Anyone whose `prev` was strictly
/// less than this AND whose `running` is >= this needs the deferral
/// nudge.
pub const CHUNKER_BUMP_VERSION: &str = "0.2.46";

/// app_state key persisting the last-seen `_CHUNKER_REVISION` sentinel string
/// (R2-4). Compared against the live value read from chunking.py each boot; a
/// mismatch triggers the re-sync deferral. This is the REVISION-crossing check
/// that the sentinel's own contract comment always promised — the
/// `CHUNKER_BUMP_VERSION` semver check above only ever fires across the one-off
/// v0.2.46 launcher-version boundary and never again for current installs, so
/// the string sentinel had no consumer until now.
pub(crate) const APP_STATE_KEY_CHUNKER_REVISION: &str = "chunker.last_seen_revision";

/// Outcome of the deferral check. Exposed for tests; production
/// callers can ignore the return value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeferralOutcome {
    /// Upgrade did not cross the chunker boundary. No deferral written.
    NoActionNeeded,
    /// Upgrade DID cross the boundary; deferral notes appended to
    /// `projects_touched` per-project UPDATE_DEFERRED.md files.
    Deferred { projects_touched: usize },
    /// Version string parsing failed. Logged on stderr; treated as
    /// no-op (the next compatible boot will retry).
    Skipped,
}

/// Compare two semver-style "X.Y.Z" strings. Returns `Ordering::Less`
/// when ``a < b``, etc. Returns `None` when either string is malformed.
/// We don't pull in `semver` for this — the launcher version strings
/// are always X.Y.Z without pre-release tags.
fn compare_versions(a: &str, b: &str) -> Option<std::cmp::Ordering> {
    let parse = |s: &str| -> Option<(u32, u32, u32)> {
        let parts: Vec<&str> = s.split('.').collect();
        if parts.len() != 3 {
            return None;
        }
        let major = parts[0].parse::<u32>().ok()?;
        let minor = parts[1].parse::<u32>().ok()?;
        let patch = parts[2].parse::<u32>().ok()?;
        Some((major, minor, patch))
    };
    let pa = parse(a)?;
    let pb = parse(b)?;
    Some(pa.cmp(&pb))
}

/// Write the chunker-resync deferral notice to one project's
/// `<project>/.claude/context/UPDATE_DEFERRED.md`.
///
/// A-5 (v0.2.73): routes through the SHARED Python emitter
/// `vco_lib.project_init._emit_chunker_resync_deferral` (which builds a proper
/// `DeferralEntry` via `DeferralReport`) instead of the previous forked
/// raw-Markdown writer. The old writer violated the shared schema: it put a
/// version pair in the severity slot (`## chunker_preset_overhaul (a → b)`),
/// which the Python parser coerced to "warning" and rewrote on any round-trip,
/// losing the version info AND all prose; it never updated the YAML
/// `condition_ids:` frontmatter, so the entry was invisible to the
/// session-start reminder nudge that is the deferral block's whole purpose.
///
/// Delegating to the single Python emitter fixes ALL of that (correct
/// condition_id `chunker_preset_overhaul_pending`, `info` severity, frontmatter
/// + reminder-splice, per-project re-sync commands) and keeps one home for the
/// deferral shape. MUST match the subprocess-into-Python pattern in
/// `storage_ux::emit_deferral` / `module_updates` (mirror-don't-fork).
fn write_deferral_for_project(
    project_folder: &Path,
    prev: &str,
    running: &str,
) -> Result<(), String> {
    let repo_root = super::installer::find_local_repo_root()
        .map_err(|e| format!("cannot locate repo root for deferral emit: {}", e))?;
    let py = pick_python()
        .ok_or_else(|| "no python on PATH to emit chunker deferral".to_string())?;

    // Pre-escape every value into Python double-quoted string literals so the
    // `-c` snippet is injection-safe (same discipline as storage_ux::emit_deferral).
    let repo_py = py_quote(&repo_root.to_string_lossy());
    let folder_py = py_quote(&project_folder.to_string_lossy());
    let prev_py = py_quote(prev);
    let running_py = py_quote(running);
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.project_init import _emit_chunker_resync_deferral\n\
         _emit_chunker_resync_deferral(Path({folder_py}), {prev_py}, {running_py})\n",
    );
    let status = std::process::Command::new(&py)
        .silent()
        .arg("-c")
        .arg(&script)
        .status();
    match status {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("chunker deferral python helper exited {}", s)),
        Err(e) => Err(format!("chunker deferral python helper spawn failed: {}", e)),
    }
}

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

/// Write the chunker-revision deferral notice to the orchestrator-root
/// project's `<project>/.claude/context/UPDATE_DEFERRED.md`. Other
/// registered projects are NOT touched — per the user's design
/// (2026-06-04), launcher-driven updates affect only the root project's
/// deferral surface. Individual project re-syncs are user-initiated
/// per-project.
///
/// Returns 1 on success, 0 when no orchestrator-root row exists or
/// when the folder is missing on disk. Soft-fails throughout.
fn write_deferral_for_root_project(db: &Db, prev: &str, running: &str) -> usize {
    let guard = db.lock();
    let folder_path_result: Result<String, rusqlite::Error> = guard.query_row(
        "SELECT folder_path FROM projects WHERE host = 'orchestrator_root' LIMIT 1",
        [],
        |row| row.get(0),
    );
    drop(guard);

    let folder_str = match folder_path_result {
        Ok(s) => s,
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            // No orchestrator-root project registered yet (first install
            // on this machine or pre-v0.2.21 install). Nothing to defer.
            return 0;
        }
        Err(e) => {
            eprintln!(
                "[chunker-deferral] SELECT orchestrator-root failed: {} — skipping",
                e
            );
            return 0;
        }
    };

    let folder = PathBuf::from(&folder_str);
    if !folder.is_dir() {
        eprintln!(
            "[chunker-deferral] orchestrator-root folder missing on disk: {} — skipping",
            folder_str
        );
        return 0;
    }

    match write_deferral_for_project(&folder, prev, running) {
        Ok(()) => 1,
        Err(e) => {
            eprintln!(
                "[chunker-deferral] write failed for {}: {} — skipping",
                folder_str, e
            );
            0
        }
    }
}

/// Public entry point called from `lib.rs::setup` AFTER the existing
/// `bust_cache_if_launcher_version_changed` hook fires with a
/// `VersionChanged` outcome.
///
/// Performs the version comparison and short-circuits when the upgrade
/// didn't cross the v0.2.46 boundary.
///
/// **Two-flow design** (locked 2026-06-04):
///
///   * Launcher-driven update (this hook): deferral lands ONLY in the
///     orchestrator-root project's UPDATE_DEFERRED.md. Other registered
///     projects are NOT touched here.
///   * Per-project bundle update (separate flow, `install-bundle --update`
///     run from inside a project folder): deferral lands in THAT
///     specific project's UPDATE_DEFERRED.md. Handled by
///     `vco_lib.project_init.install_bundle` — see its own deferral path.
pub fn write_chunker_deferral_if_crossing_boundary(
    db: &Db,
    prev: &str,
    running: &str,
) -> DeferralOutcome {
    let prev_vs_bump = match compare_versions(prev, CHUNKER_BUMP_VERSION) {
        Some(o) => o,
        None => {
            eprintln!(
                "[chunker-deferral] version parse failed: prev={}, running={} — skipping",
                prev, running
            );
            return DeferralOutcome::Skipped;
        }
    };
    let running_vs_bump = match compare_versions(running, CHUNKER_BUMP_VERSION) {
        Some(o) => o,
        None => return DeferralOutcome::Skipped,
    };

    // Upgrade crosses the boundary iff prev < v0.2.46 AND running >= v0.2.46.
    let crosses = prev_vs_bump == std::cmp::Ordering::Less
        && running_vs_bump != std::cmp::Ordering::Less;

    if !crosses {
        return DeferralOutcome::NoActionNeeded;
    }

    let touched = write_deferral_for_root_project(db, prev, running);
    eprintln!(
        "[chunker-deferral] upgrade crossed v0.2.46 boundary ({} → {}); \
         wrote re-sync notice to orchestrator-root project ({} written)",
        prev, running, touched
    );
    DeferralOutcome::Deferred {
        projects_touched: touched,
    }
}

/// Outcome of the chunker-REVISION-change check (R2-4). Parallel to
/// [`DeferralOutcome`] but keyed on the `_CHUNKER_REVISION` sentinel STRING, not
/// the launcher semver. Exposed for tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RevisionOutcome {
    /// First boot with revision-tracking (no prior revision recorded). The key
    /// is seeded to the current revision; no deferral is written. Note the
    /// limitation: an install updating from a pre-revision-tracking version may
    /// hold rows written under an OLDER chunker revision, and this seeding
    /// cannot detect that (there is no prior marker to compare) — the launcher
    /// semver boundary check covers the known historical preset change, and any
    /// later revision bump is caught from the seeded marker onward.
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
        eprintln!(
            "[chunker-revision] reader exited {} — skipping",
            output.status
        );
        return None;
    }
    let rev = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if rev.is_empty() {
        eprintln!("[chunker-revision] reader returned empty string — skipping");
        return None;
    }
    Some(rev)
}

/// Write the REVISION-change re-sync deferral to the orchestrator-root project.
/// Mirrors [`write_deferral_for_root_project`] but routes to the revision-keyed
/// Python emitter. Returns 1 on success, 0 otherwise. Soft-fails throughout.
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
            eprintln!(
                "[chunker-revision] SELECT orchestrator-root failed: {} — skipping",
                e
            );
            return 0;
        }
    };
    let folder = PathBuf::from(&folder_str);
    if !folder.is_dir() {
        eprintln!(
            "[chunker-revision] orchestrator-root folder missing on disk: {} — skipping",
            folder_str
        );
        return 0;
    }

    let repo_root = match super::installer::find_local_repo_root() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[chunker-revision] cannot locate repo root: {} — skipping", e);
            return 0;
        }
    };
    let py = match pick_python() {
        Some(p) => p,
        None => {
            eprintln!("[chunker-revision] no python to emit deferral — skipping");
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
            eprintln!("[chunker-revision] deferral python helper exited {}", s);
            0
        }
        Err(e) => {
            eprintln!("[chunker-revision] deferral python helper spawn failed: {}", e);
            0
        }
    }
}

/// Public entry point (R2-4): fire the re-sync deferral when the live
/// `_CHUNKER_REVISION` sentinel differs from the persisted last-seen value.
///
/// Called from `lib.rs::setup` on every boot (independent of the semver
/// `write_chunker_deferral_if_crossing_boundary` hook, which only ever fired
/// across the v0.2.46 launcher-version boundary). This is the consumer the
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
            eprintln!("[chunker-revision] app_state_get failed: {} — skipping", e);
            return RevisionOutcome::Skipped;
        }
    };
    match prior.as_deref() {
        None => {
            // First boot with revision-tracking: seed WITHOUT a deferral. Rows
            // already on disk were chunked under whatever revision shipped with
            // this build, so there's nothing older to re-chunk against.
            if let Err(e) = db.app_state_set(APP_STATE_KEY_CHUNKER_REVISION, &current) {
                eprintln!("[chunker-revision] seed failed: {} — next boot retries", e);
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
                eprintln!(
                    "[chunker-revision] marker advance failed: {} — deferral may re-emit (dedup'd)",
                    e
                );
            }
            eprintln!(
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
    use std::cmp::Ordering;
    use tempfile::TempDir;

    #[test]
    fn compare_versions_handles_normal_cases() {
        assert_eq!(compare_versions("0.2.45", "0.2.46"), Some(Ordering::Less));
        assert_eq!(compare_versions("0.2.46", "0.2.46"), Some(Ordering::Equal));
        assert_eq!(compare_versions("0.2.47", "0.2.46"), Some(Ordering::Greater));
        assert_eq!(compare_versions("0.3.0", "0.2.99"), Some(Ordering::Greater));
        assert_eq!(compare_versions("1.0.0", "0.99.99"), Some(Ordering::Greater));
    }

    #[test]
    fn compare_versions_rejects_malformed() {
        assert_eq!(compare_versions("", "0.2.46"), None);
        assert_eq!(compare_versions("0.2", "0.2.46"), None);
        assert_eq!(compare_versions("0.2.x", "0.2.46"), None);
        assert_eq!(compare_versions("0.2.46-dev", "0.2.46"), None);
    }

    // A-5 (v0.2.73): these tests now exercise the Python-routed emitter
    // (`vco_lib.project_init._emit_chunker_resync_deferral` via subprocess),
    // so they require `python3`/`python` on PATH + the repo root resolvable
    // from the test's CWD (both true in CI + local dev). If python is
    // unavailable the writer returns Err and the test skips its content
    // assertions rather than falsely failing — the routing contract is the
    // thing under test, and the Python emitter's OWN content is pinned by
    // `tests/test_v0247_chunker_deferral_python.py`.

    fn python_available() -> bool {
        super::pick_python().is_some()
            && super::super::installer::find_local_repo_root().is_ok()
    }

    #[test]
    fn write_deferral_for_project_creates_file_and_directory() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        // Canonical condition_id from the shared Python emitter (NOT the old
        // raw `## chunker_preset_overhaul (a → b)` header that put a version
        // pair in the severity slot).
        assert!(content.contains("chunker_preset_overhaul_pending"));
        assert!(content.contains("0.2.45"));
        assert!(content.contains("0.2.46"));
        // v0.2.75 (C-10 family fix): the emitted commands must be ones the
        // target CLIs actually accept — `kg-sync` has no `--force` and the
        // analyzer's argparse rejects `--force` (the real drop+rebuild flag
        // is `--force-recreate`). Family-wide guard:
        // tests/test_deferral_command_argparse_sweep.py.
        assert!(content.contains("kg-sync --all"));
        assert!(!content.contains("kg-sync --all --force"));
        assert!(content.contains("code-graph-analyze . --force-recreate"));
    }

    #[test]
    fn write_deferral_is_idempotent_for_same_pair() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let after_first = std::fs::read_to_string(&path).unwrap();
        // Second call: DeferralReport keys by condition_id, so re-emitting the
        // same pending condition is idempotent (no duplicate entry).
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let after_second = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            after_first.matches("chunker_preset_overhaul_pending").count(),
            after_second.matches("chunker_preset_overhaul_pending").count(),
            "re-emit must not add a duplicate condition entry"
        );
    }

    #[test]
    fn write_deferral_single_pending_condition_regardless_of_version_pairs() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.44", "0.2.46").unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.47").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        // The condition is "re-sync pending" — one condition_id, keyed once
        // regardless of the version pair that triggered it (unlike the old
        // per-pair header). `add_entry` dedups by condition_id (last-write-
        // wins), so exactly one section header exists. (Count the section
        // header, not the bare slug, which ALSO appears in the
        // `condition_ids:` frontmatter list → 2 substring matches per entry.)
        assert_eq!(
            content.matches("## chunker_preset_overhaul_pending (").count(),
            1,
            "only one pending-resync condition section should exist"
        );
    }

    #[test]
    fn check_returns_no_action_when_pre_existing_v0_2_46() {
        // No real DB needed for this code path.
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.46", "0.2.47");
        assert_eq!(outcome, DeferralOutcome::NoActionNeeded);
    }

    #[test]
    fn check_returns_no_action_for_minor_intra_v0_2_46_bump() {
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.47", "0.2.48");
        assert_eq!(outcome, DeferralOutcome::NoActionNeeded);
    }

    #[test]
    fn check_returns_deferred_when_crossing_boundary_no_projects() {
        // No projects registered; deferred outcome but touched=0.
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.45", "0.2.46");
        assert_eq!(
            outcome,
            DeferralOutcome::Deferred {
                projects_touched: 0
            }
        );
    }

    #[test]
    fn check_returns_skipped_for_malformed_versions() {
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "garbage", "0.2.46");
        assert_eq!(outcome, DeferralOutcome::Skipped);
    }

    // ---- R2-4: revision-change consumer ----------------------------------- //

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
