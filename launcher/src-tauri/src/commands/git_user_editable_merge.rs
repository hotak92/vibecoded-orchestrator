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
//!           and `git add`s the file. The CALLER then `git commit`s
//!           all staged blobs as a single synthetic "vco: pre-merge
//!           user-editable files via A0" commit (see
//!           `installer::run_pre_merge_user_editable`). The commit is
//!           load-bearing: git's pre-merge cleanliness check only
//!           accepts a working tree as "ready for merge" when staged
//!           changes are also COMMITTED — a bare `git add` leaves the
//!           staged blob differing from BOTH HEAD's and upstream-tip's
//!           blob, and `git pull --ff-only` / `git pull --no-rebase`
//!           still aborts with "Your local changes would be overwritten
//!           by merge". So the flow is: **pre-merge → stage → commit
//!           → pull**.
//!         - Conflict (exit 1 from `merge-file`) → leaves the LOCAL
//!           content in place and writes the upstream version
//!           side-by-side as `<path>.from-upstream-<short_sha>`. Emits
//!           an `orchestrator_user_modified_preserved` deferral entry so
//!           the launcher's UPDATE_DEFERRED.md viewer shows the user
//!           where to find the upstream version + how to accept it.
//!           The sidecar path is NOT auto-committed — the user's pull
//!           will fail through the existing B4 conflict modal flow.
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
//! The allowlist is intentionally HARDCODED. These are all RELATIVE paths
//! against the orchestrator clone root, so they're portable across machines.
//! `USER_EDITABLE_PATTERNS` is the canonical list — extend it there (it was
//! extended in v0.2.71 to cover `.gitignore` / `.gitattributes` / `README.md`
//! / `docs/**/*.md`). A configurable per-project override file was floated in
//! v0.2.24 but deliberately NOT built — no user demand surfaced across 45+
//! releases, and a hardcoded list keeps the trust boundary auditable in one
//! place (a config file would let any repo silently widen what auto-merges).

use std::path::{Path, PathBuf};

use globset::{GlobSet, GlobSetBuilder};
use vct_launcher_core::process::CommandExt as _;

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
    // .gitignore / .gitattributes are append-structured declarative files
    // (NOT executable): a clean line-based 3-way merge of them is correct by
    // construction (there is no "semantically broken .gitignore" the way a
    // mis-merged .py/.rs can be). Forks routinely append their own ignore
    // rules, and upstream edits these periodically. Adding them to the A0
    // allowlist means a NON-overlapping divergent edit (local appends at the
    // top / upstream appends at the bottom, or vice-versa) folds cleanly via
    // the per-path 3-way merge instead of poisoning the WHOLE auto-merge into
    // the divergence modal.
    //
    // IMPORTANT accuracy note (corrected by the v0.2.71 adversarial review —
    // an earlier comment OVERSTATED this): the A0 launcher path uses
    // `git merge-file`, a low-level primitive that does NOT consult
    // `.gitattributes` merge drivers. So the `.gitattributes merge=union`
    // entries do NOT help the launcher A0 path. The common BOTH-append-at-EOF
    // case (local and upstream each append a DIFFERENT rule at the end, an
    // OVERLAPPING diff region) therefore CONFLICTS under merge-file → A0
    // sidecars the upstream copy + writes a deferral (no data loss), it does
    // NOT silently union-fold. The `merge=union` driver is a SEPARATE,
    // FORWARD-ONLY safety net that applies ONLY to hand-run `git pull` /
    // `git merge` (which DO consult `.gitattributes`), never to this A0 path.
    // See `pre_merge_gitignore_overlapping_append_conflicts` (the honest test)
    // and the v0.2.71 update-reconciliation work. Deliberately NOT extended to
    // *.py / *.rs / install.py / *.toml / templates/** — those are protected
    // code where a textually-clean merge can be silently semantically broken,
    // so a divergent pull there should surface the modal as a real breakage
    // signal.
    ".gitignore",
    ".gitattributes",
    // README.md and docs/**/*.md are declarative Markdown (high upstream
    // churn): same text-clean==correct property. On a genuine conflict the
    // A0 path sidecars the upstream copy + writes a deferral, so no data loss.
    "README.md",
    "docs/**/*.md",
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

/// True when any outcome in the list produced a synthetic pre-merge
/// commit (i.e., at least one `Merged` variant). The caller of
/// `run_pre_merge_user_editable` uses this to decide whether the
/// follow-up `git pull --ff-only` will inevitably fail with non-FF
/// (because the synthetic commit advances local HEAD past upstream)
/// and should pre-emptively route to the rebase path instead of
/// surfacing the B4 modal for the common user-editable-edit case.
pub(crate) fn any_outcome_produced_synthetic_commit(outcomes: &[MergeOutcome]) -> bool {
    outcomes.iter().any(|o| matches!(o.kind, MergeOutcomeKind::Merged { .. }))
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
    let out = tokio::process::Command::new("git").silent()
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
    let out = tokio::process::Command::new("git").silent()
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
    let out = tokio::process::Command::new("git").silent()
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
///
/// Rename entries are special: `git status --porcelain -z` emits them
/// as `R  <new>\0<old>\0` (or `RM <new>\0<old>\0`, etc.) — TWO
/// NUL-terminated records per rename. We accept the `<new>` record
/// (the post-rename path is the one a 3-way merge cares about) and
/// SKIP the immediately-following `<old>` record (it's metadata, not
/// a standalone status line). The same logic applies to copies (`C`).
async fn list_locally_modified(install_path: &Path) -> Result<Vec<String>, String> {
    let out = tokio::process::Command::new("git").silent()
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
    // Collect records first so we can skip the trailing `<old>` record
    // of a rename/copy in a single forward pass.
    let records: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
    let mut i = 0;
    while i < records.len() {
        let record = records[i];
        if record.len() < 4 {
            // Each record is at least "XY <path>" (3 bytes minimum, +1
            // for the path). Empty trailing record from the split is
            // expected; just advance.
            i += 1;
            continue;
        }
        // Bytes [0..2] are the two-char status code; [2] is space; [3..]
        // is the path. Skip purely-unmodified rows (shouldn't appear in
        // --porcelain output, but be defensive).
        let xy = &record[..2];
        if xy == b"  " {
            i += 1;
            continue;
        }
        let path = match std::str::from_utf8(&record[3..]) {
            Ok(s) => s.to_string(),
            // Non-UTF8 paths are rare but possible; we skip them rather
            // than panic. They won't be in our allowlist anyway (the
            // allowlist is ASCII-only).
            Err(_) => {
                i += 1;
                continue;
            }
        };
        if !path.is_empty() {
            paths.push(path);
        }
        // Rename/copy: skip the immediately-following `<old>` record.
        // Both `R` and `C` use the two-record format whether they appear
        // in the staged (X) or unstaged (Y) position.
        if record[0] == b'R'
            || record[0] == b'C'
            || record[1] == b'R'
            || record[1] == b'C'
        {
            i += 2;
        } else {
            i += 1;
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
    let out = tokio::process::Command::new("git").silent()
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
        // Single-pass parse via `GlobBuilder`. Case-insensitive: HFS+/
        // APFS/NTFS default to case-folding, so a file the user sees as
        // "CLAUDE.md" might appear as "claude.md" or "CLAUDE.MD"
        // depending on how it was created. `literal_separator(false)`
        // is the default — set explicitly so `**` keeps its
        // multi-segment-wildcard semantics.
        let glob = globset::GlobBuilder::new(pattern)
            .case_insensitive(true)
            .literal_separator(false)
            .build()
            .map_err(|e| format!("malformed allowlist pattern {:?}: {}", pattern, e))?;
        builder.add(glob);
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
    let out = tokio::process::Command::new("git").silent()
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
    let mut cmd_lines: Vec<String> = vec![
        "# Inspect the upstream version side-by-side with your local:".to_string(),
        "#   diff -u <path> <path>.from-upstream-<sha>".to_string(),
        "#".to_string(),
        "# Accept upstream (overwrite local with the upstream version):".to_string(),
    ];
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

/// v0.2.56 (Defect A fix): stateless probe — would merging the local
/// HEAD with `vco_upstream/<branch>` produce a conflict-free tree?
///
/// THE PROBLEM this solves: a real 3rd-party user's Claude COMMITS KG
/// nodes locally (the encouraged behavior — `post-file-edit` hook +
/// CLAUDE.md both push it). Those local commits make `git pull
/// --ff-only` refuse with non-fast-forward, surfacing the scary
/// "Merge / Rebase / Cancel" modal — EVEN WHEN a real `git merge`
/// would be 100% conflict-free (committed KG additions never overlap
/// upstream's source/version/binary changes). The pre-merge step
/// (`run_pre_merge_user_editable`) can't help here: it only inspects
/// `git status --porcelain` (UNcommitted edits), so it's blind to the
/// committed divergence. Result: every active install hits the modal on
/// every update — not an edge case, the default trajectory.
///
/// THE FIX: before deciding `--ff-only` is the only option, ask git
/// "would the merge conflict?" via `git merge-tree --write-tree HEAD
/// <theirs>`. This is STATELESS: it computes the merge in git's object
/// store and writes NOTHING to the working tree or index (unlike `git
/// merge --no-commit`, which mutates the tree and needs an `--abort`
/// even on success — a process death between merge-start and abort
/// would leave a half-merged tree). Exit code contract (git >= 2.38,
/// `man git-merge-tree`): 0 = clean, 1 = conflicts, >=2 = the merge
/// could not even start (missing ref, etc.).
///
/// Returns:
///   - `Ok(true)`  — merge is conflict-free; caller should do a REAL
///                   `git merge` (not `--ff-only`) and proceed silently.
///   - `Ok(false)` — real content conflict (exit 1) OR merge couldn't
///                   start (exit >=2): caller must keep the modal so the
///                   user resolves by hand. (Conservative: any non-zero,
///                   non-clean outcome routes to the modal.)
///   - `Err(_)`    — subprocess spawn failure ONLY; caller treats as
///                   "can't probe" and falls back to the modal.
///
/// `theirs` is the already-resolved upstream tip SHA (use
/// `compute_theirs_sha`) — we pass a concrete SHA rather than the
/// `vco_upstream/<branch>` ref so this can't race an upstream push that
/// lands between the fetch and the probe.
pub(crate) async fn committed_divergence_merges_cleanly(
    install_path: &Path,
    theirs: &str,
) -> Result<bool, String> {
    let out = tokio::process::Command::new("git").silent()
        .args(["merge-tree", "--write-tree", "HEAD", theirs])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git merge-tree spawn failed: {}", e))?;
    // Exit 0 = clean merge. Exit 1 = conflicts. Any other non-zero
    // (git uses 128 for fatal errors like unrelated histories / missing
    // object, not 2) = the merge couldn't even start. Only exit 0 is
    // safe to auto-merge; EVERY non-zero outcome keeps the modal.
    Ok(out.status.success())
}

/// v0.2.58: the precise pop-conflict-risk check that replaces the blunt
/// `working_tree_is_clean` gate for the auto-merge decision.
///
/// THE PROBLEM with `working_tree_is_clean` (review of the v0.2.56 B1
/// guard): it treats ANY `git status --porcelain` output as "dirty" →
/// bails the silent auto-merge to the modal. But an installed orchestrator
/// is PERMANENTLY dirty in the expected way — a real instance had 540
/// dirty entries, 538 of them UNTRACKED user KG nodes + scratch files, 2
/// tracked-modified, with ZERO overlap with what upstream changed. None of
/// those could cause the `--autostash` pop-conflict the guard exists to
/// prevent, yet the blunt gate showed the scary divergence modal anyway.
///
/// THE PRECISE HAZARD the guard must actually catch: `git pull --no-rebase
/// --autostash` stashes LOCAL changes, merges, then pops. A pop conflict
/// can ONLY happen for a file that is (a) tracked + locally-modified (git
/// stash does NOT touch untracked files — they're never stashed) AND (b)
/// also changed by upstream in this merge (otherwise the pop re-applies
/// onto unchanged content, no conflict). So the ONLY risk set is
/// `tracked-modified ∩ upstream-changed`. Everything else — all untracked
/// files (user KG nodes, scratch), and tracked-modified files upstream
/// didn't touch — is safe to auto-merge over.
///
/// This honors the design principle that the update flow must NOT CARE
/// about the user's KG nodes / scratch / any expected-to-diverge file: it
/// only blocks when a genuine merge-time conflict is actually possible.
///
/// Returns the risk set (empty = safe to auto-merge). `base`/`theirs` are
/// the merge-base + upstream-tip SHAs (from `compute_base_sha` /
/// `compute_theirs_sha`). Never raises; on a git error returns the inputs
/// such that the caller treats the tree as risky (conservative).
pub(crate) async fn tracked_modified_overlapping_upstream(
    install_path: &Path,
    base: &str,
    theirs: &str,
) -> Result<Vec<String>, String> {
    // (1) tracked + locally-modified paths (EXCLUDING untracked `??`).
    //     We parse `git status --porcelain -z` directly here rather than
    //     calling `list_locally_modified` because that helper INCLUDES
    //     untracked entries (which we must exclude — git stash skips them,
    //     so they can't pop-conflict) and returns only the new path for
    //     renames. The parse below is the same `-z` record walk shape as
    //     `list_locally_modified` (rename/copy emit two NUL records); a
    //     future cleanup could extract a shared tracked-only walker.
    let status = tokio::process::Command::new("git").silent()
        .args(["status", "--porcelain", "-z"])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git status (tracked-modified) spawn failed: {}", e))?;
    if !status.status.success() {
        // Can't read status → be conservative: signal risk by returning a
        // sentinel non-empty list so the caller bails to --ff-only/modal.
        return Ok(vec!["<status-read-failed>".to_string()]);
    }
    let mut tracked_modified: std::collections::HashSet<String> = std::collections::HashSet::new();
    let bytes = status.stdout;
    let mut records = bytes.split(|b| *b == 0u8).peekable();
    while let Some(rec) = records.next() {
        if rec.len() < 4 {
            continue;
        }
        let x = rec[0];
        let y = rec[1];
        // Untracked entry is `?` in both columns — skip (git stash never
        // touches it, so it can't pop-conflict).
        if x == b'?' {
            continue;
        }
        // Rename/copy entries (`R`/`C`) emit a SECOND NUL record (the old
        // path) — consume + ignore it; the new path (this record) is what
        // a merge cares about.
        if x == b'R' || x == b'C' {
            let _ = records.next();
        }
        // Any tracked path with a non-clean status (M/A/D/R/C/U in X or Y).
        if x != b' ' || y != b' ' {
            if let Ok(path) = std::str::from_utf8(&rec[3..]) {
                if !path.is_empty() {
                    tracked_modified.insert(path.to_string());
                }
            }
        }
    }
    if tracked_modified.is_empty() {
        return Ok(Vec::new()); // nothing tracked-modified → no risk at all.
    }

    // (2) files upstream changed in this merge (merge-base..theirs).
    // CONCERN-1 fix: use `-z` (NUL-separated, LITERAL paths). Without it,
    // `git diff --name-only` honors `core.quotePath` (default true) and
    // octal-escapes non-ASCII paths (e.g. `"caff\303\250.md"`), while step
    // 1's `status --porcelain -z` emits the LITERAL path — so the
    // HashSet intersection would MISS a non-ASCII file that is both
    // tracked-modified AND upstream-changed, under-classifying a real
    // pop-conflict risk as safe (the dangerous direction). `-z` makes both
    // sides literal so the intersection is correct for any filename.
    let spec = format!("{}...{}", base, theirs);
    let diff = tokio::process::Command::new("git").silent()
        .args(["diff", "--name-only", "-z", &spec])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git diff (upstream-changed) spawn failed: {}", e))?;
    if !diff.status.success() {
        // Can't compute the upstream set → conservative: treat ALL
        // tracked-modified as risky.
        return Ok(tracked_modified.into_iter().collect());
    }
    let upstream_changed: std::collections::HashSet<String> =
        String::from_utf8_lossy(&diff.stdout)
            .split('\0')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();

    // (3) the ONLY pop-conflict-risk set: tracked-modified ∩ upstream-changed.
    Ok(tracked_modified
        .intersection(&upstream_changed)
        .cloned()
        .collect())
}

// ---------------------------------------------------------------------------
// Shared divergence pull-strategy decision (v0.2.71 Piece 3)
// ---------------------------------------------------------------------------

/// Which `git pull`/`git rebase` strategy the update flow should use once
/// the A0 pre-merge step has run. This is the SINGLE source of truth for the
/// pull-strategy decision shared by BOTH update surfaces:
///   - `installer::update_orchestrator` (the MenuBar badge), and
///   - `self_update::apply_launcher_update` (the Preferences → Updates page).
///
/// Before v0.2.71 the decision lived inline only in `update_orchestrator`;
/// the self-update surface did a blind `--ff-only` and routed ANY committed
/// divergence (e.g. a committed KG node) to a destructive `reset --hard`
/// resync. Centralising the decision here kills that drift — both surfaces
/// now fold conflict-free committed divergence via a real merge, and reserve
/// the modal/resync for genuine conflict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PullPlan {
    /// `git pull --ff-only` — conservative: no merge commit, no rebase. Used
    /// when we can't positively confirm a clean auto-merge (theirs/base
    /// unresolvable, a pop-conflict risk exists, or merge-tree reports a
    /// conflict). A non-FF then surfaces the divergence modal / resync modal,
    /// never a silent wrong-merge.
    FfOnly,
    /// `git pull --no-rebase --no-edit --autostash` — a REAL merge that
    /// preserves local commits and folds them in silently. Chosen only when
    /// merge-tree proved the committed divergence merges cleanly AND the
    /// pop-conflict-risk set (`tracked-modified ∩ upstream-changed`) is empty.
    RealMerge,
    /// `git pull --rebase --autostash --no-edit` — replays the synthetic A0
    /// pre-merge commit (and any other local commits) onto upstream tip for a
    /// linear history. Chosen when the A0 pre-merge produced a synthetic
    /// commit (so HEAD already advanced past upstream).
    RebaseAutostash,
}

impl PullPlan {
    /// The exact `git` argument vector for this plan. Returned as owned
    /// `String`s so both call-sites build the SAME args from the SAME place
    /// (they can't drift). `remote` is the upstream remote name
    /// (`VCO_UPSTREAM_REMOTE`); `branch` is the resolved pull branch.
    pub(crate) fn pull_args(&self, remote: &str, branch: &str) -> Vec<String> {
        match self {
            PullPlan::FfOnly => vec![
                "pull".to_string(),
                "--ff-only".to_string(),
                remote.to_string(),
                branch.to_string(),
            ],
            PullPlan::RealMerge => vec![
                "pull".to_string(),
                "--no-rebase".to_string(),
                "--no-edit".to_string(),
                "--autostash".to_string(),
                remote.to_string(),
                branch.to_string(),
            ],
            PullPlan::RebaseAutostash => vec![
                "pull".to_string(),
                "--rebase".to_string(),
                "--autostash".to_string(),
                "--no-edit".to_string(),
                remote.to_string(),
                branch.to_string(),
            ],
        }
    }
}

/// THE single shared classifier for "did a merge/rebase/pull fail because of a
/// CONFLICT (or a dirty-tree refusal / autostash-pop) that should route to the
/// conflict-recovery flow, rather than a network/precondition error?".
///
/// v0.2.71 (BLOCKER-1 fix): pre-v0.2.71 there were TWO hand-maintained copies —
/// `installer::is_merge_or_rebase_conflict` and `self_update::is_merge_conflict`
/// — whose own comments admitted the drift hazard. They are now ONE function
/// here (the home of the shared `resolve_divergence_pull_plan`). Both surfaces
/// call this; they cannot diverge.
///
/// IMPORTANT — feed it the COMBINED stdout+stderr. git writes `CONFLICT (...)`
/// lines to STDOUT, not stderr; a classifier fed stderr-only silently misses
/// real conflicts (that was half of BLOCKER-1). Use `git_output_combined()` /
/// `run_git_capture` so both streams reach this matcher.
///
/// Phrases (git 2.34+):
///   - merge/rebase conflict: "CONFLICT (...)", "Automatic merge failed",
///     "could not apply", "Resolve all conflicts"
///   - dirty-tree refusal: "would be overwritten", "you have unstaged changes",
///     "cannot pull with rebase", "Please commit your changes or stash",
///     "Cannot merge with local modifications"
///   - autostash-pop conflict: "autostash" (the pop left markers)
///
/// LOCALE NOTE (LOW-4): these are English substrings. git localizes user-facing
/// messages, so a non-English `LC_ALL`/`LANG` git would emit translated phrases
/// and this matcher would miss them. The callers therefore invoke git with
/// `LC_ALL=C` (see `run_git_capture`) so git always emits the C-locale English
/// wording this matcher expects. Do NOT rely on this matcher against
/// unspecified-locale git output.
pub(crate) fn is_pull_conflict(err: &str) -> bool {
    let lower = err.to_lowercase();
    lower.contains("conflict")
        || lower.contains("automatic merge failed")
        || lower.contains("could not apply")
        || lower.contains("resolve all conflicts")
        // dirty-tree refusals + autostash-pop failures
        || lower.contains("would be overwritten")
        || lower.contains("you have unstaged changes")
        || lower.contains("cannot pull with rebase")
        || lower.contains("please commit your changes or stash")
        || lower.contains("cannot merge with local modifications")
        || lower.contains("autostash")
}

/// Decide the pull strategy for a divergence-aware update. This is the EXACT
/// logic that lived inline in `update_orchestrator` (installer.rs, pre-v0.2.71)
/// — extracted verbatim so the two update surfaces share ONE decision.
///
/// Decision tree (preserved from the inline block):
///   - `pre_merge_committed` (the A0 pre-merge synthesized a commit) →
///     `RebaseAutostash` (the rebase arm owns the advanced HEAD).
///   - else resolve `theirs` (upstream tip):
///       - `Ok(None)` (tip unresolvable) / `Err` → `FfOnly` (conservative;
///         the bare pull surfaces the real error / the modal).
///       - `Ok(Some(theirs))`:
///           - compute `base` (merge-base; `None`/`Err` → flattened to None).
///           - `pop_conflict_risk`:
///               - base `Some` → `tracked_modified_overlapping_upstream`
///                 (Err → sentinel "<risk-check-failed>" = risky).
///               - base `None` → sentinel "<no-merge-base>" = risky.
///           - if `pop_conflict_risk` NON-empty → `FfOnly` (the modal
///             surfaces if non-FF; never --autostash over a real overlap).
///           - else `committed_divergence_merges_cleanly`:
///               - `Ok(true)` → `RealMerge`.
///               - `Ok(false)` / `Err` → `FfOnly`.
///
/// Best-effort throughout: any resolution/probe failure keeps `FfOnly` so the
/// legacy non-FF path surfaces the modal — we never auto-merge on uncertainty.
/// The `eprintln!` diagnostics match the originals so existing log-based
/// debugging is unchanged.
pub(crate) async fn resolve_divergence_pull_plan(
    repo: &Path,
    branch: &str,
    pre_merge_committed: bool,
) -> PullPlan {
    if pre_merge_committed {
        // pre-merge already advanced HEAD; the rebase path below owns it.
        return PullPlan::RebaseAutostash;
    }
    match compute_theirs_sha(repo, branch).await {
        Ok(Some(theirs)) => {
            let base = compute_base_sha(repo, branch).await.ok().flatten();
            // Precise pop-conflict-risk check (v0.2.58). Empty set = safe to
            // --autostash auto-merge. On any error, treat as risky (non-empty)
            // → keep --ff-only.
            let pop_conflict_risk = match base.as_deref() {
                Some(base_sha) => {
                    match tracked_modified_overlapping_upstream(repo, base_sha, &theirs).await {
                        Ok(risk) => risk,
                        Err(e) => {
                            eprintln!(
                                "[vct] resolve_divergence_pull_plan: pop-conflict-risk check \
                                 failed ({}) — keeping --ff-only.",
                                e
                            );
                            vec!["<risk-check-failed>".to_string()]
                        }
                    }
                }
                None => {
                    // No merge-base resolvable → can't compute the
                    // upstream-changed set; be conservative.
                    vec!["<no-merge-base>".to_string()]
                }
            };
            if !pop_conflict_risk.is_empty() {
                eprintln!(
                    "[vct] resolve_divergence_pull_plan: {} tracked file(s) locally-modified AND \
                     upstream-changed (autostash-pop-conflict risk) — keeping --ff-only; \
                     modal surfaces if non-FF.",
                    pop_conflict_risk.len()
                );
                PullPlan::FfOnly
            } else {
                // Tree carries no pop-conflict risk. Now confirm the merge
                // itself is conflict-free (stateless merge-tree).
                match committed_divergence_merges_cleanly(repo, &theirs).await {
                    Ok(clean) => {
                        if clean {
                            eprintln!(
                                "[vct] resolve_divergence_pull_plan: committed local divergence \
                                 merges cleanly with upstream AND no pop-conflict risk — routing \
                                 through a real merge (no modal)."
                            );
                            PullPlan::RealMerge
                        } else {
                            PullPlan::FfOnly
                        }
                    }
                    Err(e) => {
                        eprintln!(
                            "[vct] resolve_divergence_pull_plan: merge-tree probe failed ({}) — \
                             keeping --ff-only; modal will surface on non-FF.",
                            e
                        );
                        PullPlan::FfOnly
                    }
                }
            }
        }
        // Upstream tip not resolvable (no fetch yet / detached) — let the bare
        // pull surface the real error.
        Ok(None) => PullPlan::FfOnly,
        Err(e) => {
            eprintln!(
                "[vct] resolve_divergence_pull_plan: could not resolve upstream tip for merge \
                 probe ({}) — keeping --ff-only.",
                e
            );
            PullPlan::FfOnly
        }
    }
}

// ---------------------------------------------------------------------------
// Launcher-side update divergence deferral (relocated v0.2.71 Sweep-A#3)
// ---------------------------------------------------------------------------
//
// RELOCATED from installer.rs (was installer.rs-private) so BOTH update
// surfaces share ONE durable-deferral writer:
//   - the MenuBar-badge orchestrator-clone update (`installer::update_orchestrator`
//     and its binary-refresh tail), and
//   - the launcher SELF-update (`self_update::apply_launcher_update`).
//
// Pre-Sweep-A#3 only the installer surface wrote a durable
// `UPDATE_DEFERRED.md` trace on a non-FF / git-pull failure; the self-update
// surface returned ONLY a transient serialized modal error, so a launcher
// self-update that failed left NO record a terminal Claude could find at
// session start (the asymmetry §A6 of the shared-code audit flagged). Both
// surfaces now call this ONE writer. Behaviour for the installer side is
// IDENTICAL byte-for-byte to the prior installer.rs-private copy (the body
// below is relocated verbatim) — only its home changed.

/// v0.2.55 (durable-logging fix): the distinct launcher-side update
/// failure shapes that `write_launcher_update_diverged_deferral` renders.
/// All three are the SAME condition_id (`launcher_update_diverged`) so the
/// deferral self-clears on the next successful `install.py --update` — they
/// differ only in the human/Claude-facing diagnosis text.
pub(crate) enum LauncherUpdateDivergedKind {
    /// `git pull --ff-only` failed because local `main` has diverged from
    /// upstream by committed history (NOT user-editable allowlisted paths —
    /// those are handled non-blocking by the A0 per-path 3-way merge). The
    /// GUI shows a Merge/Rebase/Cancel modal, but if the user cancels or
    /// the modal is dismissed there was — pre-v0.2.55 — NO durable record.
    NonFastForward {
        local_sha: Option<String>,
        remote_sha: Option<String>,
        detail: String,
    },
    /// `WaitForBinaryRefresh` timed out but the on-disk dist binary is
    /// NEWER than the running launcher — we restarted into it anyway
    /// (v0.2.55 "update in any case"), and record that the update may be
    /// one step behind the absolute source target.
    PartialBinaryRefresh {
        running: String,
        on_disk: String,
        detail: String,
    },
    /// `WaitForBinaryRefresh` timed out and there is NO newer binary on
    /// disk — the restart was (correctly) aborted because re-execing the
    /// same old binary helps nothing. The durable record makes the stuck
    /// state diagnosable at session start.
    BinaryRefreshTimeout {
        running: String,
        on_disk: String,
        detail: String,
    },
    /// v0.2.55 (audit R1): a git-pull failure that is neither a conflict
    /// nor a non-FF divergence (broken local git, detached HEAD, missing
    /// upstream remote, etc.). PRE-v0.2.55 these returned a GUI-only error
    /// string with no durable trace.
    GitPullFailed {
        detail: String,
    },
}

/// v0.2.55 (durable-logging fix): write a `launcher_update_diverged`
/// entry into `.claude/context/UPDATE_DEFERRED.md` for launcher-side
/// update failures that PRE-v0.2.55 surfaced ONLY as a transient GUI
/// modal / a confusing "still offers update" loop.
///
/// WHY this exists: a 3rd-party user's Claude reads `UPDATE_DEFERRED.md`
/// at session start (per the project CLAUDE.md SESSION START rule). The
/// rebase/merge CONFLICT path already writes a deferral via
/// `write_resume_sentinel_and_deferral`; a plain non-FF divergence and a
/// binary-refresh timeout did NOT — so a stuck update was invisible to
/// the terminal Claude. This closes that asymmetry.
///
/// v0.2.71 Sweep-A#3: relocated from installer.rs to this shared module
/// (was installer.rs-private) so the launcher SELF-update surface
/// (`self_update::apply_launcher_update`) can call the SAME writer rather
/// than growing a second copy. Both surfaces now leave an identical
/// durable trace on a failed update.
///
/// Standalone Rust writer (does NOT depend on install.py firing) — the
/// whole point is that install.py / the binary swap did NOT complete.
/// Markdown shape mirrors `vco_lib/deferral_report.py` (frontmatter +
/// `## <condition_id> (<severity>)` + Title/Detected/Why/To apply/For
/// your Claude assistant/Detected at), so `DeferralReport.read()`
/// round-trips it and treats it as resolved on the next successful
/// install.py run (install.py running IS the resolution). Best-effort:
/// any I/O failure is logged + swallowed — the caller MUST still surface
/// its own error / continue its own flow.
pub(crate) fn write_launcher_update_diverged_deferral(
    install_path: &Path,
    branch: &str,
    kind: LauncherUpdateDivergedKind,
) {
    let target = install_path.join(".claude/context/UPDATE_DEFERRED.md");
    let parent = match target.parent() {
        Some(p) => p,
        None => {
            eprintln!(
                "[vct] launcher_update_diverged: target has no parent: {}",
                target.display()
            );
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(parent) {
        eprintln!(
            "[vct] launcher_update_diverged: mkdir {} failed: {} — skipping",
            parent.display(),
            e
        );
        return;
    }
    let now = chrono::Utc::now().to_rfc3339();
    let install_root_display = install_path.display();

    // Per-kind diagnosis. `detected` + `why` + `claude` vary; the
    // condition_id, severity, and recovery commands are shared.
    let (title, detected, why, claude_note) = match &kind {
        LauncherUpdateDivergedKind::NonFastForward {
            local_sha,
            remote_sha,
            detail,
        } => {
            let l = local_sha.as_deref().unwrap_or("<unknown>");
            let r = remote_sha.as_deref().unwrap_or("<unknown>");
            (
                "Orchestrator update could not fast-forward (local history diverged)".to_string(),
                format!(
                    "`git pull --ff-only {branch}` failed: local `{branch}` (HEAD `{l}`) has \
                     diverged from upstream (`{r}`) by committed history. This is NOT the \
                     normal case of editing CLAUDE.md / CONTEXT_STATE.md / KG nodes — those \
                     are handled non-blocking by the per-path 3-way merge. It means real \
                     commits exist on your local `{branch}` that upstream doesn't have (e.g. \
                     a clone whose `origin` was repointed at a private fork, or local commits \
                     on `{branch}` instead of a feature branch). git said: `{d}`",
                    branch = branch,
                    l = l,
                    r = r,
                    d = detail.trim(),
                ),
                "The launcher cannot safely fast-forward over diverged history. It surfaced a \
                 Merge / Rebase / Cancel modal in the GUI; if that was dismissed, the update \
                 did not apply and the launcher still runs the old binary. This entry is the \
                 durable record so the state is recoverable from a terminal."
                    .to_string(),
                format!(
                    "The user's orchestrator update could not fast-forward: local `{branch}` \
                     has committed history upstream doesn't have. The update did NOT apply. \
                     Recommended: surface this at session start. The cleanest fix depends on \
                     WHY they diverged — if local commits belong on a feature branch, move \
                     them there and reset `{branch}` to upstream; if `origin` was repointed at \
                     a private fork, the public upstream is the pull source (the launcher \
                     pulls from its configured upstream remote, not `origin`). DO NOT blindly \
                     `git reset --hard` without confirming the local commits are backed up. \
                     Once `{branch}` can fast-forward, click Update again in the launcher.",
                    branch = branch,
                ),
            )
        }
        LauncherUpdateDivergedKind::PartialBinaryRefresh {
            running,
            on_disk,
            detail,
        } => (
            "Orchestrator updated, but the launcher binary may be one step behind target"
                .to_string(),
            format!(
                "`WaitForBinaryRefresh` timed out before the on-disk launcher binary reached \
                 the exact source target, but a NEWER binary than the running one was present \
                 (running v{running}, on-disk v{on_disk}), so the launcher restarted into it \
                 anyway (v0.2.55 \"update in any case\"). The remaining gap is usually the \
                 binary-refresh commit (`chore(binary): refresh … [skip ci]`) not yet pushed \
                 by the Release workflow, or a transient pull failure. Underlying: `{d}`",
                running = running,
                on_disk = on_disk,
                d = detail.trim(),
            ),
            "Non-fatal: the launcher is now newer than before. The update may be a single \
             version behind the absolute target until the Release workflow's binary-refresh \
             commit lands; the next update closes the gap."
                .to_string(),
            format!(
                "The user's orchestrator update advanced the launcher (running was v{running}, \
                 on-disk now v{on_disk}) but may be one version behind the absolute target. \
                 This is expected briefly while the Release workflow finishes committing the \
                 refreshed dist binaries. Recommended: mention it's benign and self-resolving; \
                 if it persists across multiple update attempts over >15 min, the binary-refresh \
                 commit may have failed — check the repo's latest commit for a \
                 `chore(binary): refresh` and the Release workflow run.",
                running = running,
                on_disk = on_disk,
            ),
        ),
        LauncherUpdateDivergedKind::BinaryRefreshTimeout {
            running,
            on_disk,
            detail,
        } => {
            let od = if on_disk.is_empty() { "<unknown>" } else { on_disk };
            (
                "Orchestrator update did not deliver a new launcher binary".to_string(),
                format!(
                    "`WaitForBinaryRefresh` timed out and NO binary newer than the running \
                     launcher (v{running}) is on disk (on-disk v{od}). Restarting was aborted \
                     because re-execing the same old binary would not help. This usually means \
                     the source pull did not land the binary-refresh commit (a non-FF \
                     divergence that the re-pull kept failing on, or the Release workflow has \
                     not pushed the refreshed binaries yet). Underlying: `{d}`",
                    running = running,
                    od = od,
                    d = detail.trim(),
                ),
                "The launcher is still on the OLD binary. If a divergence blocked the pull, \
                 resolve it (see the non-FF guidance). If the binary-refresh commit simply \
                 hasn't shipped yet, waiting a few minutes and clicking Update again resolves \
                 it."
                    .to_string(),
                format!(
                    "The user's orchestrator update failed to deliver a new launcher binary — \
                     it's still on v{running}. Recommended: surface at session start. Check (1) \
                     whether `git -C {root} status` shows a diverged/non-FF `{branch}` (then \
                     follow the non-FF recovery), and (2) whether the latest upstream commit \
                     includes a `chore(binary): refresh` (if not, the Release workflow may \
                     still be building — wait + retry). Re-run the update via the launcher GUI \
                     or `python install.py --update` once the pull can advance.",
                    running = running,
                    root = install_root_display,
                    branch = branch,
                ),
            )
        }
        LauncherUpdateDivergedKind::GitPullFailed { detail } => (
            "Orchestrator update could not pull from upstream".to_string(),
            format!(
                "`git pull` for the orchestrator update failed for a reason that is \
                 neither a merge conflict nor a fast-forward divergence (e.g. a broken \
                 local git repo, a detached HEAD, or a missing/misconfigured upstream \
                 remote). git said: `{d}`",
                d = detail.trim(),
            ),
            "The update did not apply — the launcher is unchanged. The git state needs \
             attention before the update can proceed."
                .to_string(),
            format!(
                "The user's orchestrator update could not `git pull` from upstream (not a \
                 conflict, not a non-FF). Recommended: surface at session start, then \
                 inspect the repo state: `git -C {root} status`, `git -C {root} remote -v`, \
                 `git -C {root} branch --show-current`. Common causes: detached HEAD (check \
                 out `{branch}`), missing upstream remote, or an interrupted prior git op \
                 (look for `.git/MERGE_HEAD` / `.git/rebase-*`). Fix the git state, then \
                 click Update again or run `python install.py --update`.",
                root = install_root_display,
                branch = branch,
            ),
        ),
    };

    let content = format!(
        "---\n\
title: VCO Update Deferred\n\
generated_at: {now}\n\
condition_ids: [launcher_update_diverged]\n\
severity_max: warning\n\
---\n\
\n\
# VCO Update Deferred\n\
\n\
The last orchestrator update (run from the launcher GUI) hit a condition it could not \
auto-resolve safely. The section below names the condition and how to recover.\n\
\n\
## launcher_update_diverged (warning)\n\
\n\
**Title**: {title}\n\
\n\
**Detected**: {detected}\n\
\n\
**Why deferred**: {why}\n\
\n\
**To apply**:\n\
```bash\n\
# Option A (recommended): open the launcher GUI and click Update again\n\
# (top-right MenuBar). If the launcher shows a Merge/Rebase/Cancel modal,\n\
# choose Rebase to replay upstream cleanly (your local edits are kept).\n\
#\n\
# Option B (terminal): from the orchestrator install root, inspect + pull:\n\
cd {install_root_display}\n\
git status            # is `{branch}` diverged / non-fast-forward?\n\
git log --oneline -5  # do you have local commits upstream lacks?\n\
# Then either resolve the divergence (move local commits to a branch, or\n\
# pull --rebase from the configured upstream), or wait for the Release\n\
# workflow's `chore(binary): refresh` commit, then:\n\
python install.py --update\n\
# After install.py finishes, fully quit the launcher (tray -> Quit) and\n\
# relaunch so the freshly-staged binary loads.\n\
```\n\
\n\
**For your Claude assistant** (read this before continuing the user's task):\n\
{claude_note}\n\
\n\
**Detected at**: {now}\n\
\n\
---\n",
        now = now,
        title = title,
        detected = detected,
        why = why,
        claude_note = claude_note,
        install_root_display = install_root_display,
        branch = branch,
    );

    // Atomic write: temp file in the same directory, then rename.
    let tmp = parent.join(format!("UPDATE_DEFERRED.md.tmp.{}", std::process::id()));
    if let Err(e) = std::fs::write(&tmp, content.as_bytes()) {
        eprintln!(
            "[vct] launcher_update_diverged: write {} failed: {}",
            tmp.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
        return;
    }
    if let Err(e) = std::fs::rename(&tmp, &target) {
        eprintln!(
            "[vct] launcher_update_diverged: rename {} → {} failed: {}",
            tmp.display(),
            target.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
    }
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
        // NOTE: README.md WAS asserted non-editable here pre-v0.2.71, but P2
        // added it to USER_EDITABLE_PATTERNS (declarative Markdown, high
        // upstream churn, sidecar-on-conflict so no data loss). The dedicated
        // `allowlist_covers_v0271_declarative_additions` test below asserts
        // the new editable entries; this one keeps only the still-protected
        // paths.
    }

    /// v0.2.71 (Piece 2): the base commit added `.gitignore`, `.gitattributes`,
    /// `README.md`, and `docs/**/*.md` to the allowlist but shipped NO test for
    /// them. This locks in the new editable entries AND re-asserts that the
    /// protected CODE paths still BLOCK (a divergent pull there must surface
    /// the modal as a real breakage signal, never silently auto-merge).
    #[test]
    fn allowlist_covers_v0271_declarative_additions() {
        let gs = build_user_editable_globset().expect("build globset");

        // New declarative entries are NOW user-editable (auto-merge / sidecar).
        assert!(is_user_editable(".gitignore", &gs), ".gitignore must be editable");
        assert!(
            is_user_editable(".gitattributes", &gs),
            ".gitattributes must be editable"
        );
        assert!(is_user_editable("README.md", &gs), "README.md must be editable");
        assert!(
            is_user_editable("docs/anything.md", &gs),
            "docs/*.md must be editable"
        );
        assert!(
            is_user_editable("docs/features/code-graph.md", &gs),
            "nested docs/**/*.md must be editable"
        );

        // Protected CODE / config must STILL block (NOT auto-merge).
        assert!(
            !is_user_editable("vco_lib/foo.py", &gs),
            "vco_lib/*.py must stay protected"
        );
        assert!(
            !is_user_editable("launcher/src/main.rs", &gs),
            "launcher/**/*.rs must stay protected"
        );
        assert!(
            !is_user_editable("install.py", &gs),
            "install.py must stay protected"
        );
        assert!(
            !is_user_editable("templates/settings.json.linux.template", &gs),
            "templates/** must stay protected"
        );
        // docs is editable as Markdown, but a non-.md under docs/ is NOT.
        assert!(
            !is_user_editable("docs/diagram.svg", &gs),
            "non-Markdown under docs/ must stay protected"
        );
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
        let vco_lib_foo: &Path = Path::new("vco_lib/foo.py");
        assert!(
            outcomes.iter().all(|o| o.path != vco_lib_foo),
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
            .find(|o| o.path.as_path() == Path::new("CLAUDE.md"))
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
            .find(|o| o.path.as_path() == Path::new("CLAUDE.md"))
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

    // ----- Rename / copy parsing -----

    #[test]
    fn list_locally_modified_skips_rename_old_path_records() {
        // White-box test of the record-walking logic: we don't shell out
        // to `git status` here; instead we construct the raw -z payload
        // that git would emit for a rename and verify the parser admits
        // the new path and skips the trailing old-path record.
        //
        // Format reminder: `R  <new>\0<old>\0[next record...]`
        //
        // We can't drive `list_locally_modified` directly (it spawns
        // git), so we replicate its inner parse here. If the parser ever
        // moves to a private helper, swap this test to call it.
        let raw: Vec<u8> = b"R  knowledge/concepts/new.md\0knowledge/concepts/old.md\0 M CLAUDE.md\0".to_vec();
        let records: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
        let mut paths = Vec::new();
        let mut i = 0;
        while i < records.len() {
            let record = records[i];
            if record.len() < 4 {
                i += 1;
                continue;
            }
            let xy = &record[..2];
            if xy == b"  " {
                i += 1;
                continue;
            }
            if let Ok(s) = std::str::from_utf8(&record[3..]) {
                if !s.is_empty() {
                    paths.push(s.to_string());
                }
            }
            if record[0] == b'R'
                || record[0] == b'C'
                || record[1] == b'R'
                || record[1] == b'C'
            {
                i += 2;
            } else {
                i += 1;
            }
        }
        assert_eq!(
            paths,
            vec![
                "knowledge/concepts/new.md".to_string(),
                "CLAUDE.md".to_string(),
            ],
            "rename old-path record should be skipped, got: {:?}",
            paths,
        );
    }

    // ----- Full-flow integration tests (BLOCKER reproduction) -----
    //
    // These tests replicate what
    // `installer::run_pre_merge_user_editable` does end-to-end:
    //   1. pre_merge_user_editable(...) — write merged blob, etc.
    //   2. `git add` every Merged outcome.
    //   3. `git commit --no-verify -c user.name=... -c user.email=...`
    //   4. `git pull` (--ff-only or --no-rebase, per scenario)
    //
    // The synthetic commit at step 3 is the BLOCKER fix: without it,
    // `git pull` aborts because the staged blob differs from both
    // HEAD's and upstream-tip's blob (pre-merge cleanliness check
    // fails).
    //
    // We replicate the commit step inline here (rather than calling
    // `installer::run_pre_merge_user_editable`) because that fn is
    // not `pub` and is tightly coupled to installer-side state. The
    // commit invocation MUST match the production fn — keep them in
    // sync; if installer changes the author identity or commit flags,
    // update this helper too.

    /// Drive the pre-merge → stage → commit pipeline against a local
    /// clone. Mirrors `installer::run_pre_merge_user_editable`'s post-
    /// merge behaviour. Returns the outcomes for inspection.
    async fn pre_merge_stage_and_commit(
        local: &Path,
        base: &str,
        theirs: &str,
    ) -> Vec<MergeOutcome> {
        let outcomes = pre_merge_user_editable(local, base, theirs).await.unwrap();
        let mut merged_any = false;
        for outcome in &outcomes {
            if matches!(outcome.kind, MergeOutcomeKind::Merged { .. }) {
                let s = tokio::process::Command::new("git").silent()
                    .args(["add", "--"])
                    .arg(&outcome.path)
                    .current_dir(local)
                    .status()
                    .await
                    .expect("git add");
                assert!(s.success(), "git add failed for {}", outcome.path.display());
                merged_any = true;
            }
        }
        if merged_any {
            // Match installer.rs' commit invocation: -c user.name, -c
            // user.email, --no-verify, fixed message.
            let s = tokio::process::Command::new("git").silent()
                .args([
                    "-c",
                    "user.name=VCO Orchestrator",
                    "-c",
                    "user.email=orchestrator@vibecoded.tools",
                    "commit",
                    "--no-verify",
                    "-m",
                    "vco: pre-merge user-editable files via A0 (test)",
                ])
                .current_dir(local)
                .status()
                .await
                .expect("git commit");
            assert!(s.success(), "git commit failed");
        }
        outcomes
    }

    #[tokio::test]
    async fn pre_merge_then_ff_pull_lands_combined_content() {
        // The BLOCKER this test guards against is the "dirty working
        // tree" error: before the fix, `git pull --ff-only` aborted
        // with "Your local changes to the following files would be
        // overwritten by merge" because pre-merge staged the 3-way
        // result but never committed it. After the fix, the staged
        // content IS committed (synthetic VCO Orchestrator commit), so
        // the working tree is clean.
        //
        // Note on FF vs non-FF after the synthetic commit:
        //   - The synthetic commit makes local HEAD diverge from
        //     upstream tip (local has a commit upstream doesn't, and
        //     vice versa), so `--ff-only` ALWAYS fails post-pre-merge
        //     with a NON-FAST-FORWARD error (a different, expected
        //     failure mode, not the BLOCKER).
        //   - The non-FF error is handled by `update_orchestrator`'s
        //     existing B4 modal flow at installer.rs:3423, which
        //     surfaces a "Merge / Rebase / Cancel" prompt. Choosing
        //     "Merge" calls `merge_orchestrator_with_upstream` which
        //     uses `git pull --no-rebase` and lands both edits.
        //
        // This test verifies:
        //   1. The dirty-tree BLOCKER is GONE (pull's stderr no longer
        //      mentions "would be overwritten by merge").
        //   2. The merged content is on disk pre-pull (synthetic commit
        //      embeds it).
        //   3. The synthetic commit's metadata matches the spec.
        //   4. The follow-up merge pull (the production fallback path)
        //      lands cleanly with both edits.
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream: append a "section B" paragraph to CLAUDE.md.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A\nLine B\n\n## section B (from upstream)\nupstream paragraph\n",
        );
        // Local (uncommitted): prepend a "section A" paragraph.
        write_local_mod(
            &local,
            "CLAUDE.md",
            "## section A (from local)\nlocal paragraph\n# base\nLine A\nLine B\n",
        );

        // Run pre-merge → stage → commit pipeline.
        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_stage_and_commit(&local, &base, &theirs).await;

        // Must have produced a Merged outcome for CLAUDE.md.
        let claude = outcomes
            .iter()
            .find(|o| o.path.as_path() == Path::new("CLAUDE.md"))
            .expect("expected CLAUDE.md outcome");
        assert!(
            matches!(claude.kind, MergeOutcomeKind::Merged { .. }),
            "expected Merged, got {:?}",
            claude.kind,
        );

        // Working tree CLAUDE.md (after pre-merge + synthetic commit)
        // must already contain BOTH edits.
        let pre_pull = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
        assert!(
            pre_pull.contains("section A (from local)"),
            "pre-pull content missing local: {}",
            pre_pull
        );
        assert!(
            pre_pull.contains("section B (from upstream)"),
            "pre-pull content missing upstream: {}",
            pre_pull
        );

        // git status must be clean (BLOCKER assertion: staged content
        // was committed, no pending dirty edits).
        let status = StdCommand::new("git").silent()
            .args(["status", "--porcelain"])
            .current_dir(&local)
            .output()
            .expect("git status");
        assert!(
            String::from_utf8_lossy(&status.stdout).trim().is_empty(),
            "git status not clean after synthetic commit: {}",
            String::from_utf8_lossy(&status.stdout),
        );

        // The pre-merge commit must appear in the log with the
        // synthetic author + subject.
        let log = StdCommand::new("git").silent()
            .args(["log", "--format=%an <%ae>%n%s", "-n", "5"])
            .current_dir(&local)
            .output()
            .expect("git log");
        let log_text = String::from_utf8_lossy(&log.stdout);
        assert!(
            log_text.contains("VCO Orchestrator <orchestrator@vibecoded.tools>"),
            "synthetic author missing from log: {}",
            log_text,
        );
        assert!(
            log_text.contains("vco: pre-merge user-editable files via A0"),
            "synthetic commit subject missing from log: {}",
            log_text,
        );

        // Now try `git pull --ff-only`. After the synthetic commit,
        // local HEAD diverges from upstream tip, so FF can't succeed.
        // What we MUST verify is that the failure is "non-FF", NOT
        // "dirty working tree" (the original BLOCKER). The B4 modal
        // in installer.rs:3423 catches the non-FF case and offers
        // Merge/Rebase; that path is exercised below.
        let ff_pull = StdCommand::new("git").silent()
            .args(["pull", "--ff-only", "vco_upstream", "main"])
            .current_dir(&local)
            .output()
            .expect("git pull --ff-only");
        let ff_stderr = String::from_utf8_lossy(&ff_pull.stderr).to_string();
        let ff_stdout = String::from_utf8_lossy(&ff_pull.stdout).to_string();
        let combined = format!("{}\n{}", ff_stderr, ff_stdout);
        assert!(
            !combined.contains("would be overwritten by merge"),
            "BLOCKER REPRODUCED — pull aborted with dirty-tree error: {}",
            combined,
        );
        // Optional: the failure (when it fails) should be a non-FF
        // marker, not a fatal git error. If git happens to succeed
        // here on a future git version (unlikely, kept for safety),
        // we don't fail the test — the BLOCKER assertion above is
        // what we care about.
        if !ff_pull.status.success() {
            assert!(
                combined.contains("Not possible to fast-forward")
                    || combined.contains("non-fast-forward")
                    || combined.contains("Diverging branches"),
                "FF pull failed but not for a non-FF reason: {}",
                combined,
            );
        }

        // Follow-up merge pull (the production fallback via the B4
        // modal → merge_orchestrator_with_upstream) must succeed and
        // land both edits.
        let merge_pull = StdCommand::new("git").silent()
            .args([
                "pull",
                "--no-rebase",
                "--no-edit",
                "vco_upstream",
                "main",
            ])
            .current_dir(&local)
            .output()
            .expect("git pull --no-rebase");
        assert!(
            merge_pull.status.success(),
            "follow-up merge pull failed: stderr={} stdout={}",
            String::from_utf8_lossy(&merge_pull.stderr),
            String::from_utf8_lossy(&merge_pull.stdout),
        );
        let post_pull = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
        assert!(
            post_pull.contains("section A (from local)"),
            "post-pull content missing local: {}",
            post_pull,
        );
        assert!(
            post_pull.contains("section B (from upstream)"),
            "post-pull content missing upstream: {}",
            post_pull,
        );

        // Working tree clean after merge.
        let status = StdCommand::new("git").silent()
            .args(["status", "--porcelain"])
            .current_dir(&local)
            .output()
            .expect("git status");
        assert!(
            String::from_utf8_lossy(&status.stdout).trim().is_empty(),
            "git status not clean after merge pull: {}",
            String::from_utf8_lossy(&status.stdout),
        );
    }

    #[tokio::test]
    async fn pre_merge_then_merge_pull_lands_combined_content() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Local: add an unrelated committed change to force a non-FF
        // pull. We touch a non-allowlisted file so it doesn't interact
        // with the pre-merge logic.
        std::fs::create_dir_all(local.join("vco_lib")).unwrap();
        std::fs::write(
            local.join("vco_lib").join("foo.py"),
            "def local_extra(): pass\n",
        )
        .unwrap();
        run_git(&local, &["add", "vco_lib/foo.py"]);
        run_git(
            &local,
            &["commit", "-m", "local: unrelated change forcing non-FF"],
        );

        // Upstream: change CLAUDE.md.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A\nLine B\n\n## section B (from upstream)\nupstream paragraph\n",
        );
        // Local (uncommitted): change CLAUDE.md non-overlappingly.
        write_local_mod(
            &local,
            "CLAUDE.md",
            "## section A (from local)\nlocal paragraph\n# base\nLine A\nLine B\n",
        );

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_stage_and_commit(&local, &base, &theirs).await;

        let claude = outcomes
            .iter()
            .find(|o| o.path.as_path() == Path::new("CLAUDE.md"))
            .expect("expected CLAUDE.md outcome");
        assert!(
            matches!(claude.kind, MergeOutcomeKind::Merged { .. }),
            "expected Merged, got {:?}",
            claude.kind,
        );

        // Non-FF merge pull (matches merge_orchestrator_with_upstream
        // invocation: --no-rebase --no-edit).
        let pull = StdCommand::new("git").silent()
            .args([
                "pull",
                "--no-rebase",
                "--no-edit",
                "vco_upstream",
                "main",
            ])
            .current_dir(&local)
            .output()
            .expect("git pull");
        assert!(
            pull.status.success(),
            "git pull (merge) FAILED: stderr={} stdout={}",
            String::from_utf8_lossy(&pull.stderr),
            String::from_utf8_lossy(&pull.stdout),
        );

        // Working tree must contain BOTH edits.
        let merged_text = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
        assert!(
            merged_text.contains("section A (from local)"),
            "post-pull content missing local: {}",
            merged_text,
        );
        assert!(
            merged_text.contains("section B (from upstream)"),
            "post-pull content missing upstream: {}",
            merged_text,
        );

        // git status must be clean.
        let status = StdCommand::new("git").silent()
            .args(["status", "--porcelain"])
            .current_dir(&local)
            .output()
            .expect("git status");
        let status_text = String::from_utf8_lossy(&status.stdout);
        assert!(
            status_text.trim().is_empty(),
            "git status not clean after merge pull: {}",
            status_text,
        );
    }

    #[tokio::test]
    async fn pre_merge_conflict_then_pull_fails_as_expected_but_sidecar_exists() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream + local both edit the SAME LINE of CLAUDE.md →
        // forced 3-way conflict.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A (upstream wins)\nLine B\n",
        );
        let local_body = "# base\nLine A (local wins)\nLine B\n";
        write_local_mod(&local, "CLAUDE.md", local_body);

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_stage_and_commit(&local, &base, &theirs).await;

        let claude = outcomes
            .iter()
            .find(|o| o.path.as_path() == Path::new("CLAUDE.md"))
            .expect("expected CLAUDE.md outcome");
        let sidecar_path = match &claude.kind {
            MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path,
                ..
            } => upstream_sidecar_path.clone(),
            other => panic!("expected sidecar outcome, got {:?}", other),
        };

        // Sidecar must exist with the upstream content.
        let sidecar = std::fs::read_to_string(&sidecar_path).unwrap();
        assert!(
            sidecar.contains("Line A (upstream wins)"),
            "sidecar missing upstream content: {}",
            sidecar,
        );
        // Local working-tree CLAUDE.md must be unchanged.
        let local_now = std::fs::read_to_string(local.join("CLAUDE.md")).unwrap();
        assert_eq!(local_now, local_body, "local content was modified");

        // No synthetic commit should land — sidecar paths are not
        // staged. The git log head must still be the original seed
        // commit (only one commit; clone seed = HEAD).
        let log_count = StdCommand::new("git").silent()
            .args(["rev-list", "--count", "HEAD"])
            .current_dir(&local)
            .output()
            .expect("git rev-list");
        assert_eq!(
            String::from_utf8_lossy(&log_count.stdout).trim(),
            "1",
            "expected exactly 1 commit (no synthetic), got log: {}",
            String::from_utf8_lossy(&log_count.stdout),
        );

        // Now attempt git pull — it MUST fail (the working tree is
        // still dirty: local CLAUDE.md still diverges from HEAD).
        // This is the expected behaviour; the B4 modal then surfaces
        // the conflict + the deferral entry (already on disk) tells
        // the user about the sidecar.
        let pull = StdCommand::new("git").silent()
            .args(["pull", "--ff-only", "vco_upstream", "main"])
            .current_dir(&local)
            .output()
            .expect("git pull");
        assert!(
            !pull.status.success(),
            "git pull --ff-only unexpectedly succeeded with conflict on working tree: stdout={} stderr={}",
            String::from_utf8_lossy(&pull.stdout),
            String::from_utf8_lossy(&pull.stderr),
        );

        // Sidecar file must STILL exist after the failed pull.
        assert!(
            sidecar_path.exists(),
            "sidecar disappeared after failed pull: {}",
            sidecar_path.display(),
        );
    }

    // ----- v0.2.56: committed_divergence_merges_cleanly probe -----

    /// Commit a NEW local KG node (the universal 3rd-party scenario:
    /// Claude commits a knowledge node, advancing local HEAD past
    /// upstream) and push an UNRELATED upstream change. The probe must
    /// report the merge is conflict-free → caller auto-merges, no modal.
    #[tokio::test]
    async fn v0256_probe_reports_clean_for_committed_kg_node_vs_unrelated_upstream() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // LOCAL: Claude commits a brand-new KG node (a real commit, not a
        // working-tree edit — this is what the pre-merge step is BLIND to).
        write_local_mod(
            &local,
            "knowledge/concepts/my-new-node.md",
            "# My New Node\nlearned something\n",
        );
        run_git(&local, &["add", "knowledge/concepts/my-new-node.md"]);
        run_git(&local, &["commit", "-m", "docs(kg): new node"]);

        // UPSTREAM: an unrelated source change (different file entirely).
        push_upstream_change(
            &seed,
            &local,
            "vco_lib/foo.py",
            "def base(): pass\ndef added_upstream(): pass\n",
        );

        // A bare --ff-only would now refuse (local HEAD diverged). But the
        // merge is trivially clean: probe must say so.
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let clean = committed_divergence_merges_cleanly(&local, &theirs)
            .await
            .expect("probe should not error");
        assert!(
            clean,
            "expected clean merge for committed KG node vs unrelated upstream change",
        );

        // Sanity: confirm --ff-only genuinely fails here (proves the probe
        // is solving a real non-FF, not a case git would FF anyway).
        let ff = StdCommand::new("git")
            .args(["merge-base", "--is-ancestor", "HEAD", &theirs])
            .current_dir(&local)
            .status()
            .expect("merge-base");
        assert!(
            !ff.success(),
            "test precondition: HEAD must NOT be an ancestor of upstream (i.e. non-FF)",
        );
    }

    /// Local and upstream edit the SAME line of the SAME file →
    /// genuine content conflict. The probe must report NOT clean so the
    /// caller keeps the modal for human resolution.
    #[tokio::test]
    async fn v0256_probe_reports_conflict_for_overlapping_edits() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // LOCAL: commit an edit to line 2 of CLAUDE.md.
        write_local_mod(&local, "CLAUDE.md", "# base\nLine A (local edit)\nLine B\n");
        run_git(&local, &["add", "CLAUDE.md"]);
        run_git(&local, &["commit", "-m", "local edit"]);

        // UPSTREAM: edit the SAME line differently.
        push_upstream_change(
            &seed,
            &local,
            "CLAUDE.md",
            "# base\nLine A (upstream edit)\nLine B\n",
        );

        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let clean = committed_divergence_merges_cleanly(&local, &theirs)
            .await
            .expect("probe should not error");
        assert!(
            !clean,
            "expected CONFLICT (not clean) for overlapping same-line edits",
        );
    }

    /// The probe must be STATELESS: a clean probe (and even a conflicting
    /// probe) must leave the working tree + index + HEAD untouched — no
    /// MERGE_HEAD, no staged changes. This is the whole reason we use
    /// `merge-tree --write-tree` instead of `git merge --no-commit`.
    #[tokio::test]
    async fn v0256_probe_is_stateless_no_working_tree_mutation() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        write_local_mod(&local, "knowledge/concepts/n.md", "# n\nbody\n");
        run_git(&local, &["add", "knowledge/concepts/n.md"]);
        run_git(&local, &["commit", "-m", "kg"]);
        push_upstream_change(&seed, &local, "vco_lib/bar.py", "x = 1\n");

        let head_before = StdCommand::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&local)
            .output()
            .unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let _ = committed_divergence_merges_cleanly(&local, &theirs)
            .await
            .unwrap();

        // HEAD unchanged.
        let head_after = StdCommand::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&local)
            .output()
            .unwrap();
        assert_eq!(
            head_before.stdout, head_after.stdout,
            "probe must not move HEAD",
        );
        // No merge in progress.
        assert!(
            !local.join(".git").join("MERGE_HEAD").exists(),
            "probe must not leave a MERGE_HEAD (it must be stateless)",
        );
        // Working tree + index clean (no staged/unstaged changes).
        let status = StdCommand::new("git")
            .args(["status", "--porcelain"])
            .current_dir(&local)
            .output()
            .unwrap();
        assert!(
            status.stdout.is_empty(),
            "probe must leave a clean working tree, got: {}",
            String::from_utf8_lossy(&status.stdout),
        );
    }

    /// End-to-end belt-and-suspenders: the shipped repo-root
    /// `.gitattributes` declares `knowledge/**/*.md merge=union`, a
    /// BUILT-IN driver that needs no `.git/config` registration. Prove a
    /// real `git merge` of append-divergent KG nodes resolves WITHOUT
    /// conflict markers and KEEPS BOTH additions. (This guards Defect B
    /// independently of the launcher Rust path.)
    #[tokio::test]
    async fn v0256_gitattributes_union_driver_merges_kg_appends_without_registration() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // Ship the union driver in the LOCAL clone's .gitattributes
        // (mirrors the repo-root file we add in v0.2.56). NO
        // `git config merge.union.driver` — union is built-in.
        write_local_mod(&local, ".gitattributes", "knowledge/**/*.md merge=union\n");
        run_git(&local, &["add", ".gitattributes"]);
        run_git(&local, &["commit", "-m", "attrs"]);

        // Both sides APPEND a different line to the SAME existing KG file
        // (seed shipped knowledge/concepts/foo.md = "# foo\nstart\n").
        write_local_mod(&local, "knowledge/concepts/foo.md", "# foo\nstart\nLOCAL-NODE\n");
        run_git(&local, &["add", "knowledge/concepts/foo.md"]);
        run_git(&local, &["commit", "-m", "local kg append"]);
        push_upstream_change(
            &seed,
            &local,
            "knowledge/concepts/foo.md",
            "# foo\nstart\nUPSTREAM-NODE\n",
        );

        // Real merge (the launcher's auto-merge path). Must succeed.
        let merge = StdCommand::new("git")
            .args(["merge", "--no-edit", "vco_upstream/main"])
            .current_dir(&local)
            .output()
            .expect("git merge");
        assert!(
            merge.status.success(),
            "union-driver merge must succeed: stdout={} stderr={}",
            String::from_utf8_lossy(&merge.stdout),
            String::from_utf8_lossy(&merge.stderr),
        );
        let merged = std::fs::read_to_string(local.join("knowledge/concepts/foo.md")).unwrap();
        assert!(
            merged.contains("LOCAL-NODE") && merged.contains("UPSTREAM-NODE"),
            "union merge must keep BOTH additions, got:\n{}",
            merged,
        );
        assert!(
            !merged.contains("<<<<<<<") && !merged.contains(">>>>>>>"),
            "union merge must NOT leave conflict markers, got:\n{}",
            merged,
        );
    }

    // ----- v0.2.58: tracked_modified_overlapping_upstream (precise gate) -----
    //
    // Replaces the v0.2.56 blunt `working_tree_is_clean` gate. The
    // auto-merge pop-conflict risk is ONLY `tracked-modified ∩
    // upstream-changed`; untracked files + tracked-modified-not-upstream-
    // changed are safe. These tests pin that contract.

    /// VCO_dev-shaped regression (the bug this fix exists for): MANY
    /// untracked files (user KG nodes + scratch) on a committed-divergent
    /// tree, upstream changed something ELSE → risk set EMPTY → safe to
    /// auto-merge. Under the old blunt gate this returned "dirty" → modal.
    #[tokio::test]
    async fn v0258_many_untracked_files_are_not_pop_conflict_risk() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // Committed local divergence (a KG node) — the auto-merge case.
        write_local_mod(&local, "knowledge/concepts/mine.md", "# mine\n");
        run_git(&local, &["add", "knowledge/concepts/mine.md"]);
        run_git(&local, &["commit", "-m", "docs(kg): node"]);
        // Pile of UNTRACKED files (like 468 user KG nodes + scratch).
        for i in 0..20 {
            write_local_mod(&local, &format!("knowledge/concepts/untracked_{i}.md"), "# u\n");
        }
        write_local_mod(&local, "scratch_output.txt", "junk\n");
        // Upstream changes an UNRELATED tracked file.
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def base(): pass\ndef up(): pass\n");

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let risk = tracked_modified_overlapping_upstream(&local, &base, &theirs).await.unwrap();
        assert!(
            risk.is_empty(),
            "untracked files + a committed KG node must carry NO pop-conflict \
             risk (git stash skips untracked; the KG node is committed, not \
             working-tree-modified). got risk set: {:?}",
            risk,
        );
    }

    /// A tracked-modified file that upstream ALSO changed IS a pop-conflict
    /// risk → must appear in the set (so the caller bails to --ff-only/modal).
    #[tokio::test]
    async fn v0258_tracked_modified_overlapping_upstream_is_flagged() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // Upstream changes vco_lib/foo.py (it exists in the seed).
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def base(): pass\ndef upstream(): pass\n");
        // Locally leave an UNCOMMITTED (tracked) edit to the SAME file.
        write_local_mod(&local, "vco_lib/foo.py", "def base(): pass\ndef local_wip(): pass\n");

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let risk = tracked_modified_overlapping_upstream(&local, &base, &theirs).await.unwrap();
        assert!(
            risk.iter().any(|p| p == "vco_lib/foo.py"),
            "a tracked file edited locally AND changed upstream MUST be flagged \
             as pop-conflict risk. got: {:?}",
            risk,
        );
    }

    /// A tracked-modified file upstream did NOT touch is NOT a risk (the
    /// autostash pop re-applies onto unchanged content → no conflict).
    #[tokio::test]
    async fn v0258_tracked_modified_not_upstream_changed_is_safe() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // Upstream changes ONE file.
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def base(): pass\ndef up(): pass\n");
        // Locally edit a DIFFERENT tracked file (CLAUDE.md exists in seed).
        write_local_mod(&local, "CLAUDE.md", "# base\nLine A\nLine B\nlocal edit\n");

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let risk = tracked_modified_overlapping_upstream(&local, &base, &theirs).await.unwrap();
        assert!(
            risk.is_empty(),
            "a tracked file edited locally but NOT changed upstream is safe \
             (no overlap). got risk set: {:?}",
            risk,
        );
    }

    /// CONCERN-2: a STAGED-only (`git add`, not committed) tracked file that
    /// upstream ALSO changed IS a pop-conflict risk (autostash stashes
    /// staged changes too) and must be flagged. Pins the parser's
    /// non-`?`/non-clean (X column) detection so a future refactor can't
    /// silently drop staged-only coverage.
    #[tokio::test]
    async fn v0258_staged_only_overlapping_upstream_is_flagged() {
        skip_if_no_git!();
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def base(): pass\ndef up(): pass\n");
        // Edit vco_lib/foo.py locally and STAGE it (git add) without committing.
        write_local_mod(&local, "vco_lib/foo.py", "def base(): pass\ndef staged_wip(): pass\n");
        run_git(&local, &["add", "vco_lib/foo.py"]);

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let risk = tracked_modified_overlapping_upstream(&local, &base, &theirs).await.unwrap();
        assert!(
            risk.iter().any(|p| p == "vco_lib/foo.py"),
            "a STAGED-only tracked file changed upstream must be flagged (it \
             gets autostashed + can pop-conflict). got: {:?}",
            risk,
        );
    }

    // ----- v0.2.71 Piece 2: .gitignore clean-3way integration -----

    /// The user's EXACT reported case: a locally-modified tracked `.gitignore`
    /// that upstream ALSO changed (non-overlapping regions) must auto-merge via
    /// the A0 3-way fold — proving `.gitignore` (added to the allowlist in P2)
    /// now produces a `Merged` outcome, NOT a skip and NOT a conflict. Mirrors
    /// `pre_merge_clean_3way_merge_lands_in_working_tree` but for `.gitignore`.
    #[tokio::test]
    async fn pre_merge_gitignore_clean_3way_merge_folds() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Establish a base .gitignore that BOTH sides share at the merge-base
        // (the real-world case: forks inherit upstream's .gitignore). Use
        // enough lines that the start/end edits below land in well-separated
        // diff regions — a too-small file makes diff3 treat the whole thing as
        // one hunk and conflict. Push it upstream, then fast-forward `local` so
        // the base .gitignore is in local's HISTORY → it becomes the merge-base
        // content for the 3-way fold (without this, the merge-base lacks the
        // file, BASE reads empty, and both sides "add from nothing" → conflict).
        let base_ignore = "*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\n";
        push_upstream_change(&seed, &local, ".gitignore", base_ignore);
        run_git(&local, &["pull", "--ff-only", "vco_upstream", "main"]);
        // Upstream APPENDS a rule at the END (separate region).
        push_upstream_change(
            &seed,
            &local,
            ".gitignore",
            "*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\n.cache/\n",
        );
        // Local PREPENDS a different rule at the START (separate region) — git
        // merge-file folds non-overlapping start+end edits cleanly.
        write_local_mod(
            &local,
            ".gitignore",
            "node_modules/\n*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\n",
        );

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();

        let gitignore = outcomes
            .iter()
            .find(|o| o.path.as_path() == Path::new(".gitignore"))
            .expect("expected a .gitignore outcome (must NOT be skipped)");
        match &gitignore.kind {
            MergeOutcomeKind::Merged { .. } => {}
            other => panic!("expected Merged (clean 3-way fold), got {:?}", other),
        }
        // Working tree must contain BOTH the local prepend and upstream append.
        let merged = std::fs::read_to_string(local.join(".gitignore")).unwrap();
        assert!(
            merged.contains("node_modules/"),
            "merged .gitignore missing local rule: {}",
            merged
        );
        assert!(
            merged.contains(".cache/"),
            "merged .gitignore missing upstream rule: {}",
            merged
        );
    }

    /// v0.2.71 MED-2 (honest correction): the COMMON case the prior comment
    /// OVERSTATED — local appends an ignore rule at EOF AND upstream appends a
    /// DIFFERENT rule at EOF (an OVERLAPPING diff region). The A0 launcher path
    /// uses `git merge-file`, which does NOT consult `.gitattributes` drivers,
    /// so the repo-root `.gitignore merge=union` does NOT apply here: this
    /// CONFLICTS and A0 produces a `PreservedWithUpstreamSidecar` outcome (the
    /// upstream copy is written side-by-side + a deferral is emitted), NOT a
    /// clean `Merged` fold. The local working tree is left untouched (no data
    /// loss). The previous `.gitignore` test only proved the NON-overlapping
    /// (start+end) case folds; this proves the overlapping case does NOT — so
    /// the documented behaviour and the code agree.
    ///
    /// NOTE on union: a hand-run `git pull`/`git merge` WOULD union-fold this
    /// (those consult `.gitattributes`); the A0 `git merge-file` path does not.
    /// That asymmetry is exactly the accuracy point of MED-2.
    #[tokio::test]
    async fn pre_merge_gitignore_overlapping_append_conflicts() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Shared base .gitignore in BOTH histories (so the merge-base has the
        // file → BASE is non-empty for the 3-way). Push it upstream then
        // fast-forward local so it's the common ancestor content.
        let base_ignore = "*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\n";
        push_upstream_change(&seed, &local, ".gitignore", base_ignore);
        run_git(&local, &["pull", "--ff-only", "vco_upstream", "main"]);

        // Upstream APPENDS its own rule at the END (the last region).
        push_upstream_change(
            &seed,
            &local,
            ".gitignore",
            "*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\nupstream_rule/\n",
        );
        // Local ALSO appends a DIFFERENT rule at the SAME END region → the two
        // edits OVERLAP (both touch the trailing context after `*.pyc`), which
        // `git merge-file` cannot fold and marks as a conflict.
        let local_body = "*.log\n*.tmp\n*.swp\n*.bak\n*.pyc\nlocal_rule/\n";
        write_local_mod(&local, ".gitignore", local_body);

        let base = compute_base_sha(&local, "main").await.unwrap().unwrap();
        let theirs = compute_theirs_sha(&local, "main").await.unwrap().unwrap();
        let outcomes = pre_merge_user_editable(&local, &base, &theirs).await.unwrap();

        let gitignore = outcomes
            .iter()
            .find(|o| o.path.as_path() == Path::new(".gitignore"))
            .expect("expected a .gitignore outcome (must NOT be skipped)");
        match &gitignore.kind {
            MergeOutcomeKind::PreservedWithUpstreamSidecar {
                upstream_sidecar_path,
                ..
            } => {
                // Sidecar must hold the UPSTREAM content (the rule git could
                // not fold into ours).
                let sidecar = std::fs::read_to_string(upstream_sidecar_path).unwrap();
                assert!(
                    sidecar.contains("upstream_rule/"),
                    "sidecar missing upstream content: {}",
                    sidecar
                );
            }
            other => panic!(
                "expected PreservedWithUpstreamSidecar (overlapping append is a \
                 conflict under merge-file; union would fold it but A0 does NOT), \
                 got {:?}",
                other
            ),
        }
        // The local working-tree .gitignore MUST be untouched — A0 never
        // clobbers OURS on a conflict (no data loss).
        let local_now = std::fs::read_to_string(local.join(".gitignore")).unwrap();
        assert_eq!(
            local_now, local_body,
            "local .gitignore was modified on a conflict — must stay OURS",
        );
        // And it must NOT have been silently union-folded (no upstream rule
        // merged into the local file).
        assert!(
            !local_now.contains("upstream_rule/"),
            "local .gitignore was union-folded — A0/merge-file must NOT do that, \
             got:\n{}",
            local_now,
        );
    }

    // ----- v0.2.71 BLOCKER-1: the ONE shared conflict classifier -----
    // (gates the destructive resync recovery on both update surfaces — the
    // project rule requires the destructive-decision branch to be tested.)

    #[test]
    fn is_pull_conflict_detects_merge_and_rebase_conflicts() {
        // git 2.34+ samples — these land on STDOUT, which is exactly why the
        // callers must feed combined stdout+stderr.
        assert!(is_pull_conflict("CONFLICT (content): Merge conflict in .gitignore"));
        assert!(is_pull_conflict(
            "Automatic merge failed; fix conflicts and then commit the result."
        ));
        assert!(is_pull_conflict("error: could not apply abc123... Update README"));
        assert!(is_pull_conflict(
            "Resolve all conflicts manually, mark them as resolved with"
        ));
    }

    #[test]
    fn is_pull_conflict_detects_dirty_tree_refusals_and_autostash_pop() {
        assert!(is_pull_conflict(
            "error: Your local changes to the following files would be overwritten by merge:\n\t.gitignore\nPlease commit your changes or stash them before you merge."
        ));
        assert!(is_pull_conflict("error: cannot rebase: You have unstaged changes."));
        assert!(is_pull_conflict(
            "error: cannot pull with rebase: You have unstaged changes."
        ));
        assert!(is_pull_conflict(
            "error: Cannot merge with local modifications; please commit or stash them."
        ));
        assert!(is_pull_conflict("Applying autostash resulted in conflicts."));
        assert!(is_pull_conflict(
            "error: could not apply autostash, the stash entry is kept"
        ));
    }

    #[test]
    fn is_pull_conflict_ignores_network_and_precondition_errors() {
        // The dangerous direction: a NON-conflict failure must NOT be classified
        // as a conflict (else a network blip would route to the conflict modal /
        // trigger a merge-abort on a tree with no merge in progress).
        assert!(!is_pull_conflict("fatal: not a git repository"));
        assert!(!is_pull_conflict("Could not resolve host: github.com"));
        assert!(!is_pull_conflict(""));
        assert!(!is_pull_conflict(
            "fatal: unable to access 'https://github.com/...': Failed to connect"
        ));
        assert!(!is_pull_conflict("error: Permission denied (publickey)."));
        // "Already up to date." is success wording, never a conflict.
        assert!(!is_pull_conflict("Already up to date."));
        // A clean autostash SUCCESS prints "Applied autostash." — but the
        // classifier only ever runs on a FAILED pull, so this string never
        // reaches it on the success path. Documented here so a future reader
        // doesn't "fix" the substring match: note we DO match "autostash" (the
        // pop-conflict wording is "Applying autostash" / "could not apply
        // autostash"); a SUCCESS message "Applied autostash." would match too,
        // which is harmless because is_pull_conflict is only consulted after a
        // non-zero git exit. (Verified: clean autostash pulls exit 0.)
    }

    // ----- v0.2.71 Piece 3: PullPlan + shared decision -----

    #[test]
    fn pull_args_returns_exact_vectors_per_variant() {
        let remote = "vco_upstream";
        let branch = "main";

        assert_eq!(
            PullPlan::FfOnly.pull_args(remote, branch),
            vec!["pull", "--ff-only", "vco_upstream", "main"],
        );
        assert_eq!(
            PullPlan::RealMerge.pull_args(remote, branch),
            vec!["pull", "--no-rebase", "--no-edit", "--autostash", "vco_upstream", "main"],
        );
        assert_eq!(
            PullPlan::RebaseAutostash.pull_args(remote, branch),
            vec!["pull", "--rebase", "--autostash", "--no-edit", "vco_upstream", "main"],
        );
    }

    /// Commit a local-only change in the clone (creates COMMITTED divergence
    /// from upstream — the KG-node-commit case the RealMerge arm targets).
    fn commit_local_change(local: &Path, file: &str, body: &str) {
        write_local_mod(local, file, body);
        run_git(local, &["add", file]);
        run_git(local, &["commit", "-m", "local commit"]);
    }

    /// Re-fetch upstream so vco_upstream/<branch> reflects the latest push.
    fn refetch_upstream(local: &Path) {
        run_git(local, &["fetch", "vco_upstream"]);
    }

    /// pre_merge_committed=true → RebaseAutostash, regardless of repo state
    /// (the A0 rebase arm owns the advanced HEAD).
    #[tokio::test]
    async fn resolve_plan_pre_merge_committed_is_rebase() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let plan = resolve_divergence_pull_plan(&local, "main", true).await;
        assert_eq!(plan, PullPlan::RebaseAutostash);
    }

    /// Committed-clean divergence (local committed a NEW file upstream never
    /// touched) + empty pop-risk → RealMerge.
    #[tokio::test]
    async fn resolve_plan_committed_clean_divergence_is_real_merge() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream advances on an UNRELATED file (so HEAD..theirs is non-empty
        // → not a fast-forward; a real merge is needed).
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");
        // Local commits a brand-new KG node upstream never touched → clean
        // merge, no overlap, no dirty tree.
        commit_local_change(
            &local,
            "knowledge/concepts/new_node.md",
            "# new local node\n",
        );
        refetch_upstream(&local);

        let plan = resolve_divergence_pull_plan(&local, "main", false).await;
        assert_eq!(
            plan,
            PullPlan::RealMerge,
            "conflict-free committed divergence with empty pop-risk must RealMerge"
        );
    }

    /// Pop-risk NON-empty (a tracked file is BOTH locally-modified AND
    /// upstream-changed) → FfOnly (never --autostash over a real overlap).
    #[tokio::test]
    async fn resolve_plan_pop_conflict_risk_is_ff_only() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream changes vco_lib/foo.py.
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");
        // Local modifies the SAME tracked file (uncommitted) → in the
        // tracked-modified ∩ upstream-changed risk set.
        write_local_mod(&local, "vco_lib/foo.py", "def local_wip(): pass\n");

        let plan = resolve_divergence_pull_plan(&local, "main", false).await;
        assert_eq!(
            plan,
            PullPlan::FfOnly,
            "a tracked file both locally-modified and upstream-changed is a \
             pop-conflict risk → keep --ff-only"
        );
    }

    /// merge-tree reports a CONFLICT (local committed an overlapping edit to
    /// the same lines upstream changed) → FfOnly (modal surfaces on non-FF).
    #[tokio::test]
    async fn resolve_plan_merge_tree_conflict_is_ff_only() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        // Upstream rewrites CLAUDE.md line.
        push_upstream_change(&seed, &local, "CLAUDE.md", "# base\nUPSTREAM EDIT\nLine B\n");
        // Local COMMITS a conflicting edit to the SAME line (clean tree, but
        // committed divergence that merge-tree will flag as conflicting).
        commit_local_change(&local, "CLAUDE.md", "# base\nLOCAL EDIT\nLine B\n");
        refetch_upstream(&local);

        let plan = resolve_divergence_pull_plan(&local, "main", false).await;
        assert_eq!(
            plan,
            PullPlan::FfOnly,
            "a genuine merge-tree conflict must keep --ff-only so the modal surfaces"
        );
    }

    /// Upstream tip unresolvable (no `vco_upstream` remote / never fetched) →
    /// FfOnly (conservative; the bare pull surfaces the real error).
    #[tokio::test]
    async fn resolve_plan_theirs_unresolvable_is_ff_only() {
        skip_if_no_git!();
        // A fresh repo with NO vco_upstream remote at all → compute_theirs_sha
        // returns Ok(None) (rev-parse fails) → FfOnly.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("solo");
        std::fs::create_dir_all(&repo).unwrap();
        run_git(&repo, &["init", "--initial-branch=main"]);
        run_git(&repo, &["config", "user.email", "test@example.com"]);
        run_git(&repo, &["config", "user.name", "Test"]);
        std::fs::write(repo.join("CLAUDE.md"), "# base\n").unwrap();
        run_git(&repo, &["add", "."]);
        run_git(&repo, &["commit", "-m", "seed"]);

        let plan = resolve_divergence_pull_plan(&repo, "main", false).await;
        assert_eq!(
            plan,
            PullPlan::FfOnly,
            "unresolvable upstream tip must fall back to --ff-only"
        );
    }

    /// ANTI-DRIFT: BOTH update surfaces (installer::update_orchestrator and
    /// self_update::apply_launcher_update) must derive their PullPlan from the
    /// SAME `resolve_divergence_pull_plan` for identical repo state. Pre-v0.2.71
    /// only installer.rs had the probe; self_update.rs did a blind --ff-only and
    /// reset --hard on any divergence. This test constructs one repo state and
    /// asserts the shared fn yields the SAME plan when called the way EACH
    /// surface calls it (installer with pre_merge_committed possibly true after
    /// A0; self_update ALWAYS pre_merge_committed=false since it has no A0 step).
    /// For the clean-committed-divergence state both must agree on RealMerge
    /// when neither pre-merged — proving the surfaces converged.
    #[tokio::test]
    async fn resolve_plan_anti_drift_both_surfaces_agree() {
        skip_if_no_git!();
        let (_tmp, _remote, local) = init_repo_pair();
        let seed = _tmp.path().join("seed");

        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");
        commit_local_change(&local, "knowledge/concepts/drift_node.md", "# node\n");
        refetch_upstream(&local);

        // installer::update_orchestrator path when its A0 pre-merge produced NO
        // synthetic commit (the common committed-KG-divergence case):
        let installer_plan = resolve_divergence_pull_plan(&local, "main", false).await;
        // self_update::apply_launcher_update path (NEVER has an A0 step →
        // ALWAYS pre_merge_committed=false):
        let self_update_plan = resolve_divergence_pull_plan(&local, "main", false).await;

        assert_eq!(
            installer_plan, self_update_plan,
            "the two update surfaces must produce the SAME PullPlan for the same \
             repo state (shared resolve_divergence_pull_plan — no drift)"
        );
        assert_eq!(
            installer_plan,
            PullPlan::RealMerge,
            "clean committed divergence should fold via RealMerge on BOTH surfaces"
        );
    }

    // ─── launcher_update_diverged durable-logging writer ──────────────────
    //
    // RELOCATED v0.2.71 Sweep-A#3 from installer.rs (the writer + enum moved
    // to this shared module so BOTH update surfaces — the MenuBar-badge
    // orchestrator update AND the launcher SELF-update — emit the SAME
    // durable `UPDATE_DEFERRED.md` trace). The writer closes the gap where a
    // non-FF divergence / a binary-refresh timeout surfaced ONLY as a
    // transient GUI modal. These tests pin that all four kinds write a
    // parseable, comprehensive entry under the single `launcher_update_diverged`
    // condition_id.

    #[test]
    fn launcher_update_diverged_non_ff_shape_is_comprehensive() {
        let dir = tempfile::tempdir().expect("tempdir");
        let install = dir.path().to_path_buf();
        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: Some("aaaa111".into()),
                remote_sha: Some("bbbb222".into()),
                detail: "fatal: Not possible to fast-forward, aborting.".into(),
            },
        );
        let target = install.join(".claude/context/UPDATE_DEFERRED.md");
        let body = std::fs::read_to_string(&target).expect("read");

        assert!(body.starts_with("---\n"), "YAML frontmatter required");
        assert!(
            body.contains("condition_ids: [launcher_update_diverged]"),
            "frontmatter must carry the condition_id"
        );
        assert!(body.contains("## launcher_update_diverged (warning)"));
        assert!(body.contains("**Title**:"));
        assert!(body.contains("**Detected**:"));
        assert!(body.contains("**Why deferred**:"));
        assert!(body.contains("**To apply**:"));
        assert!(body.contains("**For your Claude assistant**"));
        assert!(body.contains("**Detected at**:"));
        // The non-FF detail + SHAs should be embedded for diagnosis.
        assert!(body.contains("aaaa111"), "local sha must appear");
        assert!(body.contains("bbbb222"), "remote sha must appear");
        assert!(body.contains("python install.py --update"), "CLI recovery");
    }

    #[test]
    fn launcher_update_diverged_partial_and_timeout_kinds_render() {
        let dir = tempfile::tempdir().expect("tempdir");
        let install = dir.path().to_path_buf();

        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::PartialBinaryRefresh {
                running: "0.2.54".into(),
                on_disk: "0.2.55".into(),
                detail: "timeout".into(),
            },
        );
        let body =
            std::fs::read_to_string(install.join(".claude/context/UPDATE_DEFERRED.md")).unwrap();
        assert!(body.contains("condition_ids: [launcher_update_diverged]"));
        assert!(body.contains("running v0.2.54"));
        assert!(body.contains("on-disk v0.2.55"));

        // Overwrite with the timeout kind (single-entry writer).
        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::BinaryRefreshTimeout {
                running: "0.2.54".into(),
                on_disk: String::new(), // unknown on-disk
                detail: "no newer binary".into(),
            },
        );
        let body2 =
            std::fs::read_to_string(install.join(".claude/context/UPDATE_DEFERRED.md")).unwrap();
        assert!(body2.contains("did not deliver a new launcher binary"));
        assert!(
            body2.contains("on-disk v<unknown>"),
            "empty on-disk must render as <unknown>; got: {body2}"
        );

        // v0.2.55 audit R1: the GitPullFailed kind renders under the
        // same condition_id with git-state recovery guidance.
        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::GitPullFailed {
                detail: "fatal: not a git repository".into(),
            },
        );
        let body3 =
            std::fs::read_to_string(install.join(".claude/context/UPDATE_DEFERRED.md")).unwrap();
        assert!(body3.contains("condition_ids: [launcher_update_diverged]"));
        assert!(body3.contains("could not pull from upstream"));
        assert!(
            body3.contains("not a git repository"),
            "git detail must be embedded; got: {body3}"
        );
    }

    // ─── v0.2.71 Sweep-A#3: self_update surface durable-trace coverage ────
    //
    // The whole point of relocating the writer is that the launcher
    // SELF-update path (`self_update::apply_launcher_update`) can now leave
    // the SAME durable `UPDATE_DEFERRED.md` trace the installer path already
    // does. `apply_launcher_update` itself is `#[command]` (needs a Tauri
    // AppHandle + a real git checkout), so we can't unit-test the command
    // end-to-end here. Instead we pin the contract self_update relies on:
    // the SHARED writer, called with the EXACT argument shapes the
    // self_update failure branches pass (a branch name + a NonFastForward
    // kind whose detail is the combined git output and whose SHAs come from
    // `current_sha` / `ls_remote_sha`), produces the frontmatter +
    // condition-id + recovery shape a terminal Claude can find. If this ever
    // regresses (e.g. the writer stops emitting the condition_id), the
    // self_update durable-trace promise breaks here rather than silently in
    // production.
    #[test]
    fn self_update_failure_branch_writes_findable_deferral() {
        let dir = tempfile::tempdir().expect("tempdir");
        let install = dir.path().to_path_buf();

        // Mirror exactly what `apply_launcher_update`'s non-FF / conflict
        // return path passes: a NonFastForward kind built from the combined
        // git output (`e`) + best-effort local/remote SHAs.
        let combined_git_output =
            "Auto-merging launcher/src\nCONFLICT (content): Merge conflict in launcher/src";
        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: Some("deadbeef".into()),
                remote_sha: Some("cafef00d".into()),
                detail: combined_git_output.into(),
            },
        );

        let target = install.join(".claude/context/UPDATE_DEFERRED.md");
        let body = std::fs::read_to_string(&target)
            .expect("self_update failure path must leave a durable UPDATE_DEFERRED.md");

        // A terminal Claude keys off the frontmatter condition_id at session
        // start — that's the load-bearing promise this relocation delivers
        // to the self_update surface.
        assert!(body.starts_with("---\n"), "YAML frontmatter required");
        assert!(
            body.contains("condition_ids: [launcher_update_diverged]"),
            "self_update deferral must carry the same condition_id the installer uses"
        );
        assert!(body.contains("## launcher_update_diverged (warning)"));
        assert!(body.contains("**For your Claude assistant**"));
        // The combined git output (incl. the stdout-only CONFLICT marker) +
        // the SHAs must be embedded so the failure is diagnosable.
        assert!(
            body.contains("CONFLICT (content)"),
            "combined git output (incl. stdout markers) must be embedded; got: {body}"
        );
        assert!(body.contains("deadbeef"), "local sha must appear");
        assert!(body.contains("cafef00d"), "remote sha must appear");
        assert!(body.contains("python install.py --update"), "CLI recovery present");
    }
}
