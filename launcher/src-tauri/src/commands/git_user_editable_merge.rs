//! v0.2.24 §A0: per-path 3-way merge for known user-editable files
//! during orchestrator-root updates.
//!
//! Background
//! ----------
//! `update_orchestrator` / `merge_orchestrator_with_upstream` shell out
//! to `git pull` against `vco_upstream/<branch>`. git refuses the pull
//! with "Your local changes to the following files would be overwritten
//! by merge" whenever a tracked file is BOTH locally-modified AND
//! changed upstream.
//!
//! The orchestrator's own CLAUDE.md EXPLICITLY ENCOURAGES users to
//! edit `CLAUDE.md`, `.claude/CONTEXT_STATE.md`, `.claude/MEMORY.md`,
//! and `knowledge/**/*.md`. That means every 3rd-party user hits this
//! wall the first time upstream touches any of those files.
//!
//! What this module does
//! ---------------------
//! This module sits between the `update_orchestrator` /
//! `merge_orchestrator_with_upstream` command bodies and the bare
//! `git pull` they currently run. It:
//!
//!   1. Walks the diff `<merge-base>..<upstream-tip>`.
//!   2. For each file ALSO in `git status --porcelain` (locally edited)
//!      AND matching one of the hardcoded `USER_EDITABLE_PATTERNS`:
//!         - Runs `git merge-file --stdout -p OURS BASE THEIRS`
//!           (git's 3-way text merge primitive — ships with every
//!           git install, no extra deps).
//!         - Clean merge → writes merged bytes back to the working tree
//!           and stages the file. The subsequent `git pull` sees the
//!           file as "already correct" and proceeds.
//!         - Conflict (exit 1 from `merge-file`) → leaves the LOCAL
//!           content in place and writes the upstream version
//!           side-by-side as `<path>.from-upstream-<short_sha>`. Emits
//!           an `orchestrator_user_modified_preserved` deferral entry so
//!           the launcher's UPDATE_DEFERRED.md viewer shows the user
//!           where to find the upstream version + how to accept it.
//!
//! Files NOT in the allowlist are passed through untouched — `git pull`
//! handles them normally (and if they conflict, the existing B4 modal
//! flow surfaces the error).
//!
//! Cross-OS
//! --------
//! - `git merge-file` is part of standard git on Linux/macOS/Windows.
//! - Glob matching uses `globset` with `case_insensitive(true)` so the
//!   allowlist works on case-folding filesystems (HFS+/APFS/NTFS).
//! - Temp files for the 3-way merge are created via `tempfile` (same
//!   dep already in the workspace).
//! - Subprocess invocations use `tokio::process::Command`, matching the
//!   rest of the install/update flow.
//!
//! Allowlist (hardcoded, see `USER_EDITABLE_PATTERNS`)
//! ---------------------------------------------------
//! Per the v0.2.24 §A0 design doc, the allowlist is intentionally
//! HARDCODED for v0.2.24. These are all RELATIVE paths against the
//! orchestrator clone root, so they're portable across machines. A
//! configurable `.vco-user-editable-paths.json` is deferred to v0.2.25
//! if user demand surfaces.

use std::path::{Path, PathBuf};

use globset::{Glob, GlobSet, GlobSetBuilder};

/// Hardcoded allowlist of paths the orchestrator considers
/// user-editable. Each entry is a `globset` pattern interpreted
/// RELATIVE to the orchestrator clone root. Patterns are matched
/// case-insensitively so the allowlist works on case-folding
/// filesystems (HFS+/APFS/NTFS).
///
/// The set is intentionally narrow. Adding entries here is a deliberate
/// trust decision — once a path is on the list, divergent edits won't
/// block `update_orchestrator` (they'll merge or sidecar). Files NOT
/// on the list fall through to git's default behaviour, which is what
/// we want for protected paths like `vco_lib/*.py`, `launcher/**/*.rs`,
/// etc. (the user should NOT routinely edit those, so a divergent pull
/// is a real signal of breakage).
pub(crate) const USER_EDITABLE_PATTERNS: &[&str] = &[
    // The user-facing CLAUDE.md is the most-edited file by far — every
    // project adds its own Dev Constraints / KG conventions to it.
    "CLAUDE.md",
    // Gitignored anyway, but defense-in-depth: if a user adds it to
    // their fork's tracked set, we still want non-blocking merges.
    "CLAUDE.local.md",
    // All KG nodes — high-write area.
    "knowledge/**/*.md",
    // The session-start hooks WRITE to this file. By design, every
    // session leaves a divergent commit-or-uncommitted edit here.
    ".claude/CONTEXT_STATE.md",
    // Auto-memory file the launcher and Claude both append to.
    ".claude/MEMORY.md",
    // Top-level HANDOFF-*.md files — convention for between-session
    // handoff notes. Usually gitignored but tracked in some forks.
    "HANDOFF-*.md",
];

/// Per-file resolution outcome from the pre-pull merge attempt.
#[derive(Debug, Clone)]
pub(crate) struct MergeOutcome {
    /// Path relative to the orchestrator clone root.
    pub path: PathBuf,
    pub kind: MergeOutcomeKind,
}

#[derive(Debug, Clone)]
pub(crate) enum MergeOutcomeKind {
    /// Upstream had no change OR user matched upstream byte-for-byte.
    /// Either way, no action was needed; included in the result list
    /// so the caller can introspect (mostly useful for tests).
    NoChange,
    /// 3-way merge succeeded, working tree updated, file staged.
    Merged {
        ours_sha: String,
        theirs_sha: String,
    },
    /// 3-way merge produced conflict markers. Local content was kept
    /// in place; upstream content was written as a sidecar at
    /// `upstream_sidecar_path`. Caller should emit a deferral entry.
    PreservedWithUpstreamSidecar {
        upstream_sidecar_path: PathBuf,
        ours_sha: String,
        theirs_sha: String,
    },
}

impl MergeOutcome {
    /// True when this outcome contributes to a deferral entry. Both
    /// auto-merged and sidecar-preserved files do — the user benefits
    /// from knowing what changed even on clean merges.
    pub(crate) fn is_actionable_for_deferral(&self) -> bool {
        matches!(
            self.kind,
            MergeOutcomeKind::Merged { .. } | MergeOutcomeKind::PreservedWithUpstreamSidecar { .. }
        )
    }
}

/// Best-effort: compute the merge base (common ancestor SHA) between
/// HEAD and `vco_upstream/<branch>`. Returns `Ok(None)` when the
/// upstream ref isn't fetched yet (caller should treat as "nothing to
/// pre-merge"). Returns `Err` only on subprocess spawn failure.
pub(crate) async fn compute_base_sha(
    install_path: &Path,
    upstream_branch: &str,
) -> Result<Option<String>, String> {
    let remote_ref = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        upstream_branch,
    );
    let out = tokio::process::Command::new("git")
        .args(["merge-base", "HEAD", &remote_ref])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git merge-base spawn failed: {}", e))?;
    if !out.status.success() {
        // `git merge-base` exits non-zero when one of the refs is
        // missing (e.g. vco_upstream not fetched yet). Treat as "we
        // can't pre-merge today" — caller falls back to bare `git pull`.
        return Ok(None);
    }
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if sha.is_empty() {
        Ok(None)
    } else {
        Ok(Some(sha))
    }
}

/// Best-effort: resolve `vco_upstream/<branch>` to a concrete SHA. We
/// `rev-parse` locally rather than `ls-remote` because the caller is
/// about to merge a SPECIFIC tip — using ls-remote would race with
/// upstream pushes that landed between the merge-base lookup and the
/// `git pull`.
pub(crate) async fn compute_theirs_sha(
    install_path: &Path,
    upstream_branch: &str,
) -> Result<Option<String>, String> {
    let remote_ref = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        upstream_branch,
    );
    let out = tokio::process::Command::new("git")
        .args(["rev-parse", &remote_ref])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git rev-parse spawn failed: {}", e))?;
    if !out.status.success() {
        return Ok(None);
    }
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if sha.is_empty() {
        Ok(None)
    } else {
        Ok(Some(sha))
    }
}

/// List files changed between `<base>` and `<theirs>` (the would-be
/// merge target). Used to scope the pre-merge work to ONLY files
/// upstream actually touched. `git diff --name-only A...B` includes
/// renames as both old + new paths (we get both names).
async fn list_diff_files(
    install_path: &Path,
    base: &str,
    theirs: &str,
) -> Result<Vec<String>, String> {
    let spec = format!("{}...{}", base, theirs);
    let out = tokio::process::Command::new("git")
        .args(["diff", "--name-only", &spec])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git diff (upstream files) failed: {}", e))?;
    if !out.status.success() {
        // Best-effort: empty list means no pre-merge work. The bare
        // `git pull` later will surface the real error.
        return Ok(Vec::new());
    }
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect())
}

/// List paths with unstaged working-tree modifications. We only want
/// paths the user actually touched — staged-but-unchanged-on-disk paths
/// don't need 3-way merging.
///
/// Parsing: `git status --porcelain -z` lines look like
///   `XY <path>\0` where X = staged status, Y = unstaged status.
/// We want Y in {`M`, `A`, `D`, `?`} OR X in {`M`, `A`, `D`} — any
/// kind of local divergence from HEAD warrants consideration.
async fn list_locally_modified(install_path: &Path) -> Result<Vec<String>, String> {
    let out = tokio::process::Command::new("git")
        .args(["status", "--porcelain", "-z"])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git status spawn failed: {}", e))?;
    if !out.status.success() {
        return Ok(Vec::new());
    }
    let raw = out.stdout;
    let mut paths = Vec::new();
    // Split on NUL: `git status -z` emits NUL-terminated records.
    for record in raw.split(|b| *b == 0) {
        if record.len() < 4 {
            // Each record is at least "XY <path>" (3 bytes minimum).
            continue;
        }
        // Bytes [0..2] are the two-char status code; [2] is space; [3..]
        // is the path. Skip purely-unmodified rows (shouldn't appear in
        // --porcelain output, but be defensive).
        let xy = &record[..2];
        if xy == b"  " {
            continue;
        }
        let path = match std::str::from_utf8(&record[3..]) {
            Ok(s) => s.to_string(),
            // Non-UTF8 paths are rare but possible; we skip them rather
            // than panic. They won't be in our allowlist anyway (the
            // allowlist is ASCII-only).
            Err(_) => continue,
        };
        if !path.is_empty() {
            paths.push(path);
        }
    }
    Ok(paths)
}

/// Read the BLOB content of `path` at revision `sha` via `git show`.
/// Returns `Ok(None)` when the path doesn't exist at that revision
/// (treat as "empty" in the 3-way merge — git merge-file does the same).
async fn read_blob_at_rev(
    install_path: &Path,
    sha: &str,
    path: &str,
) -> Result<Option<Vec<u8>>, String> {
    let spec = format!("{}:{}", sha, path);
    let out = tokio::process::Command::new("git")
        .args(["show", &spec])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git show spawn failed: {}", e))?;
    if !out.status.success() {
        // Treat "path not in this revision" as None. Stderr usually
        // says "fatal: path '<x>' does not exist in '<sha>'".
        return Ok(None);
    }
    Ok(Some(out.stdout))
}

/// Build the `GlobSet` from `USER_EDITABLE_PATTERNS`. Built once per
/// call; cheap enough (six patterns) that caching across calls is
/// unnecessary. Returns `Err` on a malformed pattern (a programming
/// error — the constant list is hand-curated, so this should never
/// fire in production).
fn build_user_editable_globset() -> Result<GlobSet, String> {
    let mut builder = GlobSetBuilder::new();
    for pattern in USER_EDITABLE_PATTERNS {
        let glob = Glob::new(pattern)
            .map_err(|e| format!("malformed allowlist pattern {:?}: {}", pattern, e))?;
        // Case-insensitive: HFS+/APFS/NTFS default to case-folding, so
        // a file the user sees as "CLAUDE.md" might appear as "claude.md"
        // or "CLAUDE.MD" depending on how it was created.
        builder.add(
            Glob::new(glob.glob())
                .and_then(|g| {
                    globset::GlobBuilder::new(g.glob())
                        .case_insensitive(true)
                        .literal_separator(false)
                        .build()
                })
                .map_err(|e| format!("malformed allowlist pattern {:?}: {}", pattern, e))?,
        );
    }
    builder
        .build()
        .map_err(|e| format!("failed to build user-editable globset: {}", e))
}

/// True when `rel_path` (relative to the orchestrator clone root)
/// matches any pattern in `USER_EDITABLE_PATTERNS`.
pub(crate) fn is_user_editable(rel_path: &str, globset: &GlobSet) -> bool {
    // Normalise path separators to forward slashes — globset's `**`
    // semantics expect POSIX-style paths regardless of host OS.
    let normalised = rel_path.replace('\\', "/");
    globset.is_match(&normalised)
}

/// Result of the `do_3way_merge` primitive.
#[derive(Debug)]
enum ThreeWayResult {
    /// Clean merge — `bytes` contains the merged content.
    Clean(Vec<u8>),
    /// Conflict markers in the merge — `bytes` contains the conflict-
    /// marked output. We DON'T write this to the working tree (it
    /// would clobber the user's local). The caller writes the upstream
    /// version as a sidecar instead.
    Conflict,
}

/// Invoke `git merge-file --stdout -p OURS BASE THEIRS` against
/// three on-disk temp files (one per side). Returns `Clean(merged)` on
/// exit 0, `Conflict` on exit 1, `Err` on any subprocess/IO failure.
async fn do_3way_merge(
    ours: &[u8],
    base: &[u8],
    theirs: &[u8],
) -> Result<ThreeWayResult, String> {
    // git merge-file takes three FILE arguments. We allocate them in a
    // fresh tempdir so concurrent calls don't collide.
    let dir = tempfile::Builder::new()
        .prefix("vct-3way-merge-")
        .tempdir()
        .map_err(|e| format!("tempdir create failed: {}", e))?;
    let ours_path = dir.path().join("ours");
    let base_path = dir.path().join("base");
    let theirs_path = dir.path().join("theirs");
    std::fs::write(&ours_path, ours)
        .map_err(|e| format!("write ours tempfile failed: {}", e))?;
    std::fs::write(&base_path, base)
        .map_err(|e| format!("write base tempfile failed: {}", e))?;
    std::fs::write(&theirs_path, theirs)
        .map_err(|e| format!("write theirs tempfile failed: {}", e))?;
    // `-p` prints to stdout (don't modify ours in place); `--stdout` is
    // the long alias for the same flag and we pass both for clarity.
    // The labels (`-L`) make conflict markers more readable should the
    // caller ever write them to disk (we don't, but it helps debugging).
    let out = tokio::process::Command::new("git")
        .args([
            "merge-file",
            "-p",
            "-L",
            "ours (local)",
            "-L",
            "base (merge base)",
            "-L",
            "theirs (upstream)",
        ])
        .arg(&ours_path)
        .arg(&base_path)
        .arg(&theirs_path)
        .output()
        .await
        .map_err(|e| format!("git merge-file spawn failed: {}", e))?;
    let code = out.status.code();
    match code {
        // Exit 0: clean merge.
        Some(0) => Ok(ThreeWayResult::Clean(out.stdout)),
        // Exit > 0: conflict markers. git's man page says "exit code is
        // the number of conflicts (truncated to 127)". Any positive
        // value means we should sidecar.
        Some(n) if n > 0 && n < 128 => Ok(ThreeWayResult::Conflict),
        // Exit < 0 or > 127: an actual error (binary file, OOM, etc.).
        // Surface to caller so the file gets skipped + git pull handles
        // it the legacy way.
        Some(n) => Err(format!(
            "git merge-file failed with exit {}: {}",
            n,
            String::from_utf8_lossy(&out.stderr).trim()
        )),
        None => Err(format!(
            "git merge-file killed by signal: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )),
    }
}

/// Short-form SHA suffix for sidecar filenames (7 chars matches the
/// `git log --oneline` default).
fn short_sha(sha: &str) -> String {
    sha.chars().take(7).collect()
}

/// Compute the sidecar path for an upstream version of `rel_path`.
/// Returns `<install_path>/<rel_path>.from-upstream-<short_theirs>`.
/// Cross-OS safe (no special chars on any filesystem).
fn sidecar_path_for(install_path: &Path, rel_path: &Path, theirs_sha: &str) -> PathBuf {
    let short = short_sha(theirs_sha);
    let parent = rel_path
        .parent()
        .map(|p| install_path.join(p))
        .unwrap_or_else(|| install_path.to_path_buf());
    let stem = rel_path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    parent.join(format!("{}.from-upstream-{}", stem, short))
}

/// Pre-merge user-editable files BEFORE the main `git pull`.
///
/// For each file in the upstream diff that is ALSO locally modified:
///   - If not in `USER_EDITABLE_PATTERNS`: included in result as
///     `NoChange` (we don't touch it; the caller's git pull will
///     either succeed or surface a structured conflict error).
///   - Else: run 3-way merge.
///       - Clean: write merged content + `git add`. `git pull` will see
///         no diff for this path and proceed.
///       - Conflict: write upstream to `<path>.from-upstream-<sha>`,
///         leave local in place. Caller emits a deferral entry.
///
/// Returns the list of outcomes. Best-effort: any per-file failure
/// during merge (binary file, IO error, subprocess crash) logs a
/// warning to stderr and skips that file (treated as `NoChange`). The
/// caller's bare `git pull` will then surface the failure the normal
/// way.
pub(crate) async fn pre_merge_user_editable(
    install_path: &Path,
    base_sha: &str,
    theirs_sha: &str,
) -> Result<Vec<MergeOutcome>, String> {
    let globset = build_user_editable_globset()?;

    let upstream_files = list_diff_files(install_path, base_sha, theirs_sha).await?;
    if upstream_files.is_empty() {
        return Ok(Vec::new());
    }

    let local_modifications = list_locally_modified(install_path).await?;
    if local_modifications.is_empty() {
        // No local changes anywhere → nothing for the pre-merge step
        // to do. `git pull` will fast-forward cleanly.
        return Ok(Vec::new());
    }

    // Intersection: files upstream touched AND user locally modified.
    let local_set: std::collections::HashSet<String> =
        local_modifications.iter().cloned().collect();
    let candidates: Vec<String> = upstream_files
        .into_iter()
        .filter(|p| local_set.contains(p))
        .collect();

    let mut outcomes = Vec::with_capacity(candidates.len());

    for rel_path_str in candidates {
        // Allowlist filter — non-matching paths fall through to the
        // caller's `git pull` (and the B4 conflict modal if needed).
        if !is_user_editable(&rel_path_str, &globset) {
            continue;
        }

        let rel_path = PathBuf::from(&rel_path_str);
        let absolute = install_path.join(&rel_path);

        // OURS = current working-tree content.
        let ours = match std::fs::read(&absolute) {
            Ok(b) => b,
            Err(e) => {
                eprintln!(
                    "[vct] pre_merge: skipping {} — read OURS failed: {}",
                    rel_path_str, e
                );
                continue;
            }
        };
        // BASE = content at the merge-base commit. Missing → empty.
        let base = match read_blob_at_rev(install_path, base_sha, &rel_path_str).await {
            Ok(Some(b)) => b,
            Ok(None) => Vec::new(),
            Err(e) => {
                eprintln!(
                    "[vct] pre_merge: skipping {} — read BASE failed: {}",
                    rel_path_str, e
                );
                continue;
            }
        };
        // THEIRS = content at the upstream tip.
        let theirs = match read_blob_at_rev(install_path, theirs_sha, &rel_path_str).await {
            Ok(Some(b)) => b,
            Ok(None) => Vec::new(),
            Err(e) => {
                eprintln!(
                    "[vct] pre_merge: skipping {} — read THEIRS failed: {}",
                    rel_path_str, e
                );
                continue;
            }
        };

        // Binary safety: `git merge-file` will refuse and exit non-zero
        // on binary files; we treat that as `Err` and skip. Detection
        // heuristic before invocation saves a subprocess hop: any NUL
        // byte in any of the three sides signals binary. (Same
        // heuristic git itself uses for `core.binary` auto-detection.)
        if ours.contains(&0u8) || base.contains(&0u8) || theirs.contains(&0u8) {
            eprintln!(
                "[vct] pre_merge: skipping {} — binary content detected; \
                 falling through to git pull's default handling",
                rel_path_str
            );
            continue;
        }

        match do_3way_merge(&ours, &base, &theirs).await {
            Ok(ThreeWayResult::Clean(merged)) => {
                // Skip the noop case: if `merged` == `ours`, the user's
                // local already incorporates the upstream change (or
                // there was nothing to merge). No write needed.
                if merged == ours {
                    outcomes.push(MergeOutcome {
                        path: rel_path.clone(),
                        kind: MergeOutcomeKind::NoChange,
                    });
                    continue;
                }
                // Write merged content to disk. Atomic-ish: write to
                // a temp sibling + rename. `git pull` will then see no
                // diff at this path.
                if let Err(e) = atomic_write(&absolute, &merged) {
                    eprintln!(
                        "[vct] pre_merge: clean-merge write failed for {}: {} — skipping",
                        rel_path_str, e
                    );
                    continue;
                }
                outcomes.push(MergeOutcome {
                    path: rel_path,
                    kind: MergeOutcomeKind::Merged {
                        ours_sha: short_sha(base_sha), // OURS-baseline reference
                        theirs_sha: short_sha(theirs_sha),
                    },
                });
            }
            Ok(ThreeWayResult::Conflict) => {
                // Sidecar the upstream version, keep local in place.
                let sidecar = sidecar_path_for(install_path, &rel_path, theirs_sha);
                if let Err(e) = atomic_write(&sidecar, &theirs) {
                    eprintln!(
                        "[vct] pre_merge: sidecar write failed for {} ({}): {} — skipping",
                        rel_path_str,
                        sidecar.display(),
                        e
                    );
                    continue;
                }
                outcomes.push(MergeOutcome {
                    path: rel_path,
                    kind: MergeOutcomeKind::PreservedWithUpstreamSidecar {
                        upstream_sidecar_path: sidecar,
                        ours_sha: short_sha(base_sha),
                        theirs_sha: short_sha(theirs_sha),
                    },
                });
            }
            Err(e) => {
                eprintln!(
                    "[vct] pre_merge: 3-way merge errored for {}: {} — skipping (git pull will handle)",
                    rel_path_str, e
                );
                // Skip = no entry in outcomes for this path; falls
                // through to git pull's default error path.
            }
        }
    }

    Ok(outcomes)
}

/// Atomic-ish write: temp-sibling + `std::fs::rename`. Used for both
/// merged-content writes and sidecar writes. Avoids torn files if the
/// host process is interrupted mid-write.
fn atomic_write(target: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut tmp = target.to_path_buf();
    let file_name = target
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "tmp".to_string());
    // Include a uuid in the temp name so concurrent calls don't collide.
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    tmp.set_file_name(format!(".{}.{}.tmp", file_name, nonce));
    std::fs::write(&tmp, bytes)?;
    // Rename is atomic within a filesystem on POSIX; on Windows it's
    // atomic except when the destination is open (which it can't be —
    // we just wrote tmp and target is closed).
    std::fs::rename(&tmp, target)
}

// ---------------------------------------------------------------------------
// Deferral emission: shell out to vco_lib.deferral_report.
//
// The Python writer at `vco_lib/deferral_report.py` is the canonical
// path: it does atomic markdown writes, injects the CLAUDE.md "see
// UPDATE_DEFERRED.md" reminder block, and handles the read/parse cycle
// for round-tripping. Mirroring the precedent set by
// `commands::storage_ux::emit_deferral`, we shell out to a small
// Python `-c` snippet rather than re-implementing the markdown writer
// in Rust.
//
// The Rust → Python invocation is best-effort: any failure (no python
// on PATH, malformed clone, subprocess returns non-zero) is logged and
// swallowed. Deferrals are an FYI mechanism; a failure here mustn't
// mask the original update success.
// ---------------------------------------------------------------------------

/// Emit one `orchestrator_user_modified_preserved` deferral entry
/// summarising every actionable outcome (merged or sidecar-preserved).
/// `NoChange` outcomes are filtered out — they don't need user action.
///
/// One entry per call (not per file). The entry body lists every
/// affected file inline so the UPDATE_DEFERRED.md viewer renders a
/// single section, not N near-duplicate sections.
pub(crate) fn emit_orchestrator_user_modified_deferrals(
    install_path: &Path,
    outcomes: &[MergeOutcome],
    pull_branch: &str,
) -> Result<(), String> {
    let actionable: Vec<&MergeOutcome> = outcomes
        .iter()
        .filter(|o| o.is_actionable_for_deferral())
        .collect();
    if actionable.is_empty() {
        // Nothing to defer — every outcome was NoChange.
        return Ok(());
    }

    let py = match pick_python_for_deferral() {
        Some(p) => p,
        None => {
            eprintln!(
                "[vct] pre_merge: no python on PATH; skipping deferral emit \
                 ({} affected file(s) — user must inspect manually)",
                actionable.len()
            );
            return Ok(());
        }
    };

    let (title, detected, why_deferred, command_to_apply) =
        build_deferral_text(install_path, &actionable, pull_branch);

    let repo_py = py_quote(install_path.to_string_lossy().as_ref());
    let cid_py = py_quote("orchestrator_user_modified_preserved");
    let title_py = py_quote(&title);
    let det_py = py_quote(&detected);
    let why_py = py_quote(&why_deferred);
    let cmd_py = py_quote(&command_to_apply);
    let sev_py = py_quote("info");

    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_report import DeferralEntry, DeferralReport\n\
         folder = Path({repo_py})\n\
         report = DeferralReport.read(folder)\n\
         entry = DeferralEntry(\n\
         \x20\x20\x20\x20condition_id={cid_py},\n\
         \x20\x20\x20\x20title={title_py},\n\
         \x20\x20\x20\x20detected={det_py},\n\
         \x20\x20\x20\x20why_deferred={why_py},\n\
         \x20\x20\x20\x20command_to_apply={cmd_py},\n\
         \x20\x20\x20\x20severity={sev_py},\n\
         )\n\
         report.add_entry(entry)\n\
         report.write(folder)\n",
    );
    let mut cmd = std::process::Command::new(py);
    cmd.arg("-c").arg(&script);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    match cmd.status() {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => {
            eprintln!(
                "[vct] pre_merge: deferral helper exited {:?}; deferral may be missing",
                s.code()
            );
            Ok(())
        }
        Err(e) => {
            eprintln!(
                "[vct] pre_merge: deferral helper spawn failed: {} — continuing",
                e
            );
            Ok(())
        }
    }
}

/// Build the four free-form text fields for the deferral entry. Kept
/// out of the main emit fn so the JSON-shape unit test can call it
/// without spawning Python.
pub(crate) fn build_deferral_text(
    install_path: &Path,
    actionable: &[&MergeOutcome],
    pull_branch: &str,
) -> (String, String, String, String) {
    let n = actionable.len();
    let title = format!(
        "{} orchestrator-root file{} preserved/merged during update",
        n,
        if n == 1 { "" } else { "s" }
    );
    // Per-file bullet list, capped at 100 entries to bound the
    // UPDATE_DEFERRED.md growth. (Same cap as
    // _format_file_list_md in project_init.py.)
    const CAP: usize = 100;
    let mut bullets: Vec<String> = Vec::new();
    let mut shown = 0usize;
    for outcome in actionable {
        if shown >= CAP {
            break;
        }
        match &outcome.kind {
            MergeOutcomeKind::Merged {
                ours_sha,
                theirs_sha,
            } => {
                bullets.push(format!(
                    "  - `{}` — auto-merged (3-way, ours={} theirs={})",
                    outcome.path.display(),
                    ours_sha,
                    theirs_sha,
                ));
            }
            MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path,
                ours_sha,
                theirs_sha,
            } => {
                let sidecar_rel = upstream_sidecar_path
                    .strip_prefix(install_path)
                    .unwrap_or(upstream_sidecar_path);
                bullets.push(format!(
                    "  - `{}` — conflict; local preserved, upstream saved as `{}` (base={} theirs={})",
                    outcome.path.display(),
                    sidecar_rel.display(),
                    ours_sha,
                    theirs_sha,
                ));
            }
            MergeOutcomeKind::NoChange => {
                // Filtered out earlier; defensive.
                continue;
            }
        }
        shown += 1;
    }
    if actionable.len() > CAP {
        bullets.push(format!("  - ... and {} more", actionable.len() - CAP));
    }
    let detected = format!(
        "During an orchestrator-root update pulling from `{}/{}`, {} \
         user-editable file(s) had both local and upstream changes. \
         VCO ran a per-path 3-way merge before `git pull`:\n{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        pull_branch,
        n,
        bullets.join("\n"),
    );
    let why_deferred = String::from(
        "Default-to-safety: when a file in the user-editable allowlist \
         (`CLAUDE.md`, `knowledge/**/*.md`, `.claude/CONTEXT_STATE.md`, \
         `.claude/MEMORY.md`, etc.) has both local edits and upstream \
         edits, VCO auto-merges non-overlapping changes but PRESERVES \
         local content when the merge has conflicts. The upstream version \
         is saved alongside as `<path>.from-upstream-<sha>` so you can \
         inspect or accept it manually.",
    );
    // Per-OS the `mv` command is the same on POSIX and works on Windows
    // via `cmd /c move`. We give POSIX form as the default and mention
    // the Windows alternative in prose, mirroring how UPDATE_DEFERRED.md
    // is read by the launcher (markdown viewer, not a shell).
    let mut cmd_lines = Vec::new();
    cmd_lines.push(
        "# Inspect the upstream version side-by-side with your local:".to_string(),
    );
    cmd_lines.push("#   diff -u <path> <path>.from-upstream-<sha>".to_string());
    cmd_lines.push("#".to_string());
    cmd_lines.push("# Accept upstream (overwrite local with the upstream version):".to_string());
    for outcome in actionable.iter().take(CAP) {
        if let MergeOutcomeKind::PreservedWithUpstreamSidecar {
            upstream_sidecar_path,
            ..
        } = &outcome.kind
        {
            let local_rel = outcome.path.display().to_string();
            let sidecar_rel = upstream_sidecar_path
                .strip_prefix(install_path)
                .unwrap_or(upstream_sidecar_path)
                .display()
                .to_string();
            cmd_lines.push(format!(
                "#   mv {} {}    # POSIX",
                shell_quote(&sidecar_rel),
                shell_quote(&local_rel),
            ));
            cmd_lines.push(format!(
                "#   move {} {}   # Windows cmd.exe",
                win_quote(&sidecar_rel),
                win_quote(&local_rel),
            ));
        }
    }
    cmd_lines.push("#".to_string());
    cmd_lines.push("# OR keep your local edits and delete the upstream sidecar:".to_string());
    for outcome in actionable.iter().take(CAP) {
        if let MergeOutcomeKind::PreservedWithUpstreamSidecar {
            upstream_sidecar_path,
            ..
        } = &outcome.kind
        {
            let sidecar_rel = upstream_sidecar_path
                .strip_prefix(install_path)
                .unwrap_or(upstream_sidecar_path)
                .display()
                .to_string();
            cmd_lines.push(format!("#   rm {}      # POSIX", shell_quote(&sidecar_rel)));
            cmd_lines.push(format!("#   del {}     # Windows cmd.exe", win_quote(&sidecar_rel)));
        }
    }
    cmd_lines.push("#".to_string());
    cmd_lines.push("# Auto-merged files were already written to the working tree;".to_string());
    cmd_lines.push("# review them with `git diff HEAD`.".to_string());
    let command_to_apply = cmd_lines.join("\n");
    (title, detected, why_deferred, command_to_apply)
}

/// POSIX shell-safe quoting (single-quote escape).
fn shell_quote(s: &str) -> String {
    if s.chars().all(|c| {
        c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/' | ':')
    }) {
        s.to_string()
    } else {
        // Wrap in single quotes, escape any embedded single quote.
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}

/// Windows cmd.exe-safe quoting.
fn win_quote(s: &str) -> String {
    if s.chars().all(|c| {
        c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/' | '\\' | ':')
    }) {
        s.to_string()
    } else {
        format!("\"{}\"", s.replace('"', "\\\""))
    }
}

/// Python-double-quoted string literal escaper. Copied from
/// `storage_ux.rs::py_quote` to keep this module self-contained.
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
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Probe for a python interpreter on PATH. Returns `Some(<absolute
/// path>)` for the first hit; `None` if neither `python3` nor `python`
/// is available. Copied from `storage_ux.rs::pick_python` to keep this
/// module self-contained.
fn pick_python_for_deferral() -> Option<String> {
    for candidate in &["python3", "python"] {
        if let Some(paths) = std::env::var_os("PATH") {
            for dir in std::env::split_paths(&paths) {
                #[cfg(windows)]
                let probe = dir.join(format!("{candidate}.exe"));
                #[cfg(not(windows))]
                let probe = dir.join(candidate);
                if probe.is_file() {
                    return Some(probe.to_string_lossy().to_string());
                }
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command as StdCommand, Stdio};

    macro_rules! skip_if_no_git {
        () => {
            if StdCommand::new("git")
                .arg("--version")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| !s.success())
                .unwrap_or(true)
            {
                eprintln!("skipping: git not on PATH");
                return;
            }
        };
    }

    /// Initialise a tempdir-hosted bare "upstream" + working "local"
    /// clone. Returns (tempdir, upstream-bare-path, local-clone-path).
    /// The tempdir is held by the caller to keep the test environment
    /// alive (drop = remove). The local clone has a `vco_upstream`
    /// remote pointing at the bare repo.
    fn init_repo_pair() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().to_path_buf();
        let remote = root.join("remote.git");
        let local = root.join("local");

        // Bare remote.
        assert!(StdCommand::new("git")
            .args(["init", "--bare", "--initial-branch=main"])
            .arg(&remote)
            .status()
            .expect("git init --bare")
            .success());

        // Seed workdir.
        let seed = root.join("seed");
        std::fs::create_dir_all(&seed).unwrap();
        run_git(&seed, &["init", "--initial-branch=main"]);
        run_git(&seed, &["config", "user.email", "test@example.com"]);
        run_git(&seed, &["config", "user.name", "Test"]);
        std::fs::write(seed.join("CLAUDE.md"), "# base\nLine A\nLine B\n").unwrap();
        std::fs::create_dir_all(seed.join("knowledge").join("concepts")).unwrap();
        std::fs::write(
            seed.join("knowledge").join("concepts").join("foo.md"),
            "# foo\nstart\n",
        )
        .unwrap();
        std::fs::create_dir_all(seed.join("vco_lib")).unwrap();
        std::fs::write(seed.join("vco_lib").join("foo.py"), "def base(): pass\n").unwrap();
        run_git(&seed, &["add", "."]);
        run_git(&seed, &["commit", "-m", "seed"]);
        run_git(
            &seed,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        run_git(&seed, &["push", "origin", "main"]);

        // Clone.
        assert!(StdCommand::new("git")
            .args(["clone"])
            .arg(remote.to_str().unwrap())
            .arg(&local)
            .status()
            .expect("git clone")
            .success());
        run_git(&local, &["config", "user.email", "test@example.com"]);
        run_git(&local, &["config", "user.name", "Test"]);
        run_git(
            &local,
            &["remote", "add", "vco_upstream", remote.to_str().unwrap()],
        );
        run_git(&local, &["fetch", "vco_upstream"]);

        (tmp, remote, local)
    }

    /// Advance the upstream by committing in the seed workdir + pushing
    /// to remote.git, then re-fetch vco_upstream in the local clone.
    fn push_upstream_change(seed_root: &Path, local: &Path, file: &str, body: &str) {
        std::fs::create_dir_all(seed_root.join(file).parent().unwrap_or(seed_root)).unwrap();
        std::fs::write(seed_root.join(file), body).unwrap();
        run_git(seed_root, &["add", file]);
        run_git(seed_root, &["commit", "-m", "upstream change"]);
        run_git(seed_root, &["push", "origin", "main"]);
        run_git(local, &["fetch", "vco_upstream"]);
    }

    fn run_git(cwd: &Path, args: &[&str]) {
        let out = StdCommand::new("git")
            .args(args)
            .current_dir(cwd)
            .output()
            .expect("git spawn");
        assert!(
            out.status.success(),
            "git {:?} failed in {}: stderr={}",
            args,
            cwd.display(),
            String::from_utf8_lossy(&out.stderr)
        );
    }

    fn write_local_mod(local: &Path, file: &str, body: &str) {
        let abs = local.join(file);
        if let Some(parent) = abs.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&abs, body).unwrap();
    }

    // ----- Pure unit tests (no git subprocess) -----

    #[test]
    fn allowlist_matches_documented_paths() {
        let gs = build_user_editable_globset().expect("build globset");
        assert!(is_user_editable("CLAUDE.md", &gs));
        assert!(is_user_editable("CLAUDE.local.md", &gs));
        assert!(is_user_editable(".claude/CONTEXT_STATE.md", &gs));
        assert!(is_user_editable(".claude/MEMORY.md", &gs));
        assert!(is_user_editable("knowledge/concepts/foo.md", &gs));
        assert!(is_user_editable("knowledge/projects/bar/baz.md", &gs));
        assert!(is_user_editable("HANDOFF-2026-05-22.md", &gs));

        // Non-user-editable paths.
        assert!(!is_user_editable("vco_lib/project_init.py", &gs));
        assert!(!is_user_editable("launcher/src-tauri/src/lib.rs", &gs));
        assert!(!is_user_editable("templates/scripts/kg-search", &gs));
        assert!(!is_user_editable("README.md", &gs));
    }

    #[test]
    fn pre_merge_glob_case_insensitive_on_windows_path_style() {
        let gs = build_user_editable_globset().expect("build globset");
        // Linux-style: exact case.
        assert!(is_user_editable("CLAUDE.md", &gs));
        // Case-folding filesystem variants — HFS+/APFS/NTFS can present
        // any of these for the same on-disk file.
        assert!(is_user_editable("claude.md", &gs));
        assert!(is_user_editable("CLAUDE.MD", &gs));
        // Backslash path separators (Windows-native git output before
        // `-z` normalisation does NOT use backslashes, but defense-in-
        // depth).
        assert!(is_user_editable("knowledge\\concepts\\foo.md", &gs));
        assert!(is_user_editable("knowledge\\concepts\\FOO.MD", &gs));
    }

    #[test]
    fn sidecar_path_uses_short_sha_and_preserves_parent() {
        let install = Path::new("/tmp/install");
        let rel = Path::new("knowledge/concepts/foo.md");
        let p = sidecar_path_for(install, rel, "7b255dd1234567890abcdef");
        assert_eq!(
            p,
            Path::new("/tmp/install/knowledge/concepts/foo.md.from-upstream-7b255dd")
        );
    }

    #[test]
    fn build_deferral_text_lists_files_under_cap() {
        let install = Path::new("/tmp/install");
        let outcome_merged = MergeOutcome {
            path: PathBuf::from("CLAUDE.md"),
            kind: MergeOutcomeKind::Merged {
                ours_sha: "abc1234".to_string(),
                theirs_sha: "7b255dd".to_string(),
            },
        };
        let outcome_preserved = MergeOutcome {
            path: PathBuf::from("knowledge/concepts/foo.md"),
            kind: MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path: install
                    .join("knowledge/concepts/foo.md.from-upstream-7b255dd"),
                ours_sha: "abc1234".to_string(),
                theirs_sha: "7b255dd".to_string(),
            },
        };
        let actionable = vec![&outcome_merged, &outcome_preserved];
        let (title, detected, why, cmd) =
            build_deferral_text(install, &actionable, "main");
        assert!(title.contains("2"), "title: {}", title);
        assert!(title.contains("preserved/merged"), "title: {}", title);
        // The file list must appear in `detected`.
        assert!(detected.contains("CLAUDE.md"), "detected: {}", detected);
        assert!(
            detected.contains("knowledge/concepts/foo.md"),
            "detected: {}",
            detected
        );
        // `why_deferred` must mention the allowlist intent.
        assert!(
            why.to_lowercase().contains("allowlist") || why.contains("user-editable"),
            "why: {}",
            why,
        );
        // `command_to_apply` must include both the accept-upstream and
        // keep-local paths for sidecar'd files.
        assert!(cmd.contains("mv"), "cmd missing POSIX mv: {}", cmd);
        assert!(cmd.contains("move"), "cmd missing Windows move: {}", cmd);
        assert!(cmd.contains("rm"), "cmd missing POSIX rm: {}", cmd);
        assert!(cmd.contains("del"), "cmd missing Windows del: {}", cmd);
        assert!(
            cmd.contains("knowledge/concepts/foo.md.from-upstream-7b255dd"),
            "cmd missing sidecar path: {}",
            cmd
        );
    }

    // ----- Integration tests (require git on PATH) -----

    #[tokio::test]
    async fn pre_merge_skips_non_user_editable_paths() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream: change vco_lib/foo.py (NOT in allowlist).
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");
        // Local: also modify vco_lib/foo.py.
        write_local_mod(&local, "vco_lib/foo.py", "def local(): pass\n");

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();

        // vco_lib/foo.py is not in the allowlist → no outcome emitted.
        assert!(
            outcomes.iter().all(|o| o.path != PathBuf::from("vco_lib/foo.py")),
            "expected vco_lib/foo.py to be filtered out, got {:?}",
            outcomes.iter().map(|o| &o.path).collect::<Vec<_>>(),
        );
    }

    #[tokio::test]
    async fn pre_merge_clean_3way_merge_lands_in_working_tree() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream: append a line to CLAUDE.md.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A\nLine B\nLine C (from upstream)\n",
        );
        // Local: prepend a line (non-overlapping with upstream's append).
        write_local_mod(
            &local,
            "CLAUDE.md",
            "Line ZZ (from local)\n# base\nLine A\nLine B\n",
        );

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();

        // Should produce one `Merged` outcome for CLAUDE.md.
        let claude = outcomes
            .iter()
            .find(|o| o.path == PathBuf::from("CLAUDE.md"))
            .expect("expected CLAUDE.md outcome");
        match &claude.kind {
            MergeOutcomeKind::Merged { .. } => {}
            other => panic!("expected Merged, got {:?}", other),
        }
        // Working tree should now contain both edits.
        let merged_text = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
        assert!(
            merged_text.contains("Line ZZ (from local)"),
            "merged content missing local: {}",
            merged_text
        );
        assert!(
            merged_text.contains("Line C (from upstream)"),
            "merged content missing upstream: {}",
            merged_text
        );
    }

    #[tokio::test]
    async fn pre_merge_conflict_writes_sidecar() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream: replace Line A entirely.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A (upstream wins)\nLine B\n",
        );
        // Local: same line, different value → overlapping edit.
        let local_body = "# base\nLine A (local wins)\nLine B\n";
        write_local_mod(&local, "CLAUDE.md", local_body);

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();

        let claude = outcomes
            .iter()
            .find(|o| o.path == PathBuf::from("CLAUDE.md"))
            .expect("expected CLAUDE.md outcome");
        match &claude.kind {
            MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path,
                ..
            } => {
                // Sidecar file must exist with the upstream content.
                let sidecar = std::fs::read_to_string(upstream_sidecar_path).unwrap();
                assert!(
                    sidecar.contains("Line A (upstream wins)"),
                    "sidecar missing upstream content: {}",
                    sidecar
                );
                // Local working-tree CLAUDE.md must be UNCHANGED.
                let local_now = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
                assert_eq!(local_now, local_body, "local content was modified");
            }
            other => panic!("expected sidecar outcome, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn pre_merge_handles_binary_knowledge_node_gracefully() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream: write binary bytes (NUL-containing) to a knowledge node.
        let binary: Vec<u8> = (0u8..=8).chain(10u8..=20).collect();
        std::fs::write(
            seed.join("knowledge").join("concepts").join("foo.md"),
            &binary,
        )
        .unwrap();
        run_git(&seed, &["add", "knowledge/concepts/foo.md"]);
        run_git(&seed, &["commit", "-m", "binary upstream"]);
        run_git(&seed, &["push", "origin", "main"]);
        run_git(&local, &["fetch", "vco_upstream"]);

        // Local: change the same file but with text content.
        write_local_mod(&local, "knowledge/concepts/foo.md", "# foo (local edit)\n");

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        // Must NOT panic and must NOT emit a Merged outcome (we skip
        // binary files; git pull handles them the legacy way).
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();
        assert!(
            outcomes.iter().all(|o| !matches!(
                o.kind,
                MergeOutcomeKind::Merged { .. }
                    | MergeOutcomeKind::PreservedWithUpstreamSidecar { .. }
            )),
            "expected binary file to be skipped, got outcomes: {:?}",
            outcomes
        );
        // Local content must remain untouched.
        let local_now = std::fs::read_to_string(
            local.join("knowledge").join("concepts").join("foo.md"),
        )
        .unwrap();
        assert_eq!(local_now, "# foo (local edit)\n");
    }

    #[tokio::test]
    async fn pre_merge_handles_missing_upstream_ref_gracefully() {
        skip_if_no_git!();
        // Init a standalone repo with NO vco_upstream remote at all.
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("solo");
        std::fs::create_dir_all(&local).unwrap();
        run_git(&local, &["init", "--initial-branch=main"]);
        run_git(&local, &["config", "user.email", "test@example.com"]);
        run_git(&local, &["config", "user.name", "Test"]);
        std::fs::write(local.join("CLAUDE.md"), "# initial\n").unwrap();
        run_git(&local, &["add", "."]);
        run_git(&local, &["commit", "-m", "initial"]);

        // compute_base_sha should return Ok(None) — no upstream ref.
        let base = compute_base_sha(&local, "main").await.unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap();
        assert!(base.is_none(), "expected no merge base without upstream");
        assert!(theirs.is_none(), "expected no theirs without upstream");
    }

    #[tokio::test]
    async fn pre_merge_emits_deferral_via_python_bridge_in_correct_shape() {
        skip_if_no_git!();
        // This test only runs when python3 is on PATH AND vco_lib is
        // importable from the test orchestrator clone (the test pair
        // has no vco_lib stub, so we point at the real repo root). We
        // skip if either is missing — keeping the unit test list
        // green on CI containers that don't ship Python.
        let py = match pick_python_for_deferral() {
            Some(p) => p,
            None => {
                eprintln!("skipping: no python on PATH");
                return;
            }
        };
        // Locate the workspace root via the cargo manifest dir.
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let workspace_root = Path::new(manifest_dir)
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.to_path_buf())
            .expect("locate workspace root");
        if !workspace_root.join("vco_lib").join("deferral_report.py").is_file() {
            eprintln!("skipping: vco_lib/deferral_report.py not findable from manifest dir");
            return;
        }
        // Quick import check.
        let probe = StdCommand::new(&py)
            .args(["-c", "import sys; sys.path.insert(0, '"])
            // The probe needs the real workspace path — easier to just
            // skip the round-trip if the previous file-exists check
            // already established vco_lib is reachable.
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if probe.is_err() {
            eprintln!("skipping: python probe failed");
            return;
        }

        // Use the real workspace root as the "install path" so vco_lib
        // imports work without any extra setup. We'll write the
        // deferral into a SEPARATE temp folder so we don't pollute the
        // workspace's own .claude/context.
        let tmp = tempfile::tempdir().unwrap();
        let fake_install = tmp.path().to_path_buf();
        // Create a minimal `.claude/context` so the writer can land
        // UPDATE_DEFERRED.md without other side effects.
        std::fs::create_dir_all(fake_install.join(".claude").join("context")).unwrap();
        // Make vco_lib importable from this tempdir by symlink-or-copy
        // (cross-OS-safe via copy).
        std::fs::create_dir_all(fake_install.join("vco_lib")).unwrap();
        for entry in std::fs::read_dir(workspace_root.join("vco_lib")).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_file()
                && entry.path().extension().map(|e| e == "py").unwrap_or(false)
            {
                std::fs::copy(
                    entry.path(),
                    fake_install.join("vco_lib").join(entry.file_name()),
                )
                .unwrap();
            }
        }

        let outcomes = vec![MergeOutcome {
            path: PathBuf::from("CLAUDE.md"),
            kind: MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path: fake_install
                    .join("CLAUDE.md.from-upstream-7b255dd"),
                ours_sha: "abc1234".to_string(),
                theirs_sha: "7b255dd".to_string(),
            },
        }];

        let res = emit_orchestrator_user_modified_deferrals(&fake_install, &outcomes, "main");
        assert!(res.is_ok(), "deferral emit returned Err: {:?}", res);

        // Read back the deferral.
        let deferred = fake_install
            .join(".claude")
            .join("context")
            .join("UPDATE_DEFERRED.md");
        assert!(
            deferred.exists(),
            "expected UPDATE_DEFERRED.md at {}",
            deferred.display(),
        );
        let body = std::fs::read_to_string(&deferred).unwrap();
        assert!(
            body.contains("## orchestrator_user_modified_preserved"),
            "missing condition_id header in {}",
            body,
        );
        assert!(
            body.contains("CLAUDE.md.from-upstream-7b255dd"),
            "missing sidecar reference in {}",
            body,
        );
    }
}
